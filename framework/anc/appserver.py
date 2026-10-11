"""Codex 0.154 stdio transport. One reader, disjoint client/server request IDs.

The host owns authentication. Model tool arguments never select a capability.
No retry of a turn or tool request is performed by this transport.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import threading
import time


class HostDisconnected(RuntimeError):
    pass


class HostRejected(RuntimeError):
    def __init__(self, method, error):
        super().__init__('App-server rejected ' + method)
        self.protocol_error = error


class AppServer:
    def __init__(self, argv, environment, cwd, stderr_path, notification, request):
        self.lock = threading.Lock()
        self.ready = threading.Condition()
        self.responses, self.pending = {}, set()
        self.next_id, self.dead = 1, False
        self.notification, self.request = notification, request
        self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix='anc-tool')
        Path(stderr_path).parent.mkdir(parents=True, exist_ok=True)
        self.stderr = Path(stderr_path).open('ab', buffering=0)
        env = os.environ.copy()
        # Task or API credentials inherited by the controller never enter the host.
        for key in ('ANC_TASK_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY'):
            env.pop(key, None)
        env.update(environment)
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr, cwd=cwd, env=env, text=True, encoding='utf-8', bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.job_handle = None
        try:
            self._bind_lifetime()
        except BaseException:
            self.process.kill()
            self.process.wait(timeout=10)
            self.process.stdin.close()
            self.process.stdout.close()
            self.stderr.close()
            self.executor.shutdown(wait=False, cancel_futures=True)
            raise
        self.reader = threading.Thread(target=self._read, name='anc-codex-reader', daemon=True)
        self.reader.start()
        try:
            self.rpc('initialize', {'clientInfo': {'name': 'anc-runtime', 'version': '1'},
                                    'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
        except BaseException:
            self.close()
            raise

    def _bind_lifetime(self):
        """Windows closes the entire host on controller death, including abrupt death.

        Assignment happens before initialization or any model request. A refused
        assignment fails closed instead of leaving an independent auth writer.
        """
        if os.name != 'nt':
            return
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong), ('LimitFlags', w.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', w.DWORD), ('Affinity', ctypes.c_size_t),
                ('PriorityClass', w.DWORD), ('SchedulingClass', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                ('ReadOperationCount','WriteOperationCount','OtherOperationCount',
                 'ReadTransferCount','WriteTransferCount','OtherTransferCount')]
        class Extended(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', Basic), ('IoInfo', IO),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.CloseHandle.argtypes = [w.HANDLE]
        handle = k.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if (not handle or not k.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                or not k.AssignProcessToJobObject(handle, w.HANDLE(int(self.process._handle)))):
            if handle:
                k.CloseHandle(handle)
            raise OSError(ctypes.get_last_error(), 'Host lifetime containment unavailable')
        self.job_handle = handle

    def send(self, message):
        with self.lock:
            if self.dead or self.process.poll() is not None:
                raise HostDisconnected('Authentication host disconnected')
            self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(',', ':')) + '\n')
            self.process.stdin.flush()

    def rpc(self, method, params=None, timeout=30):
        with self.ready:
            rid = self.next_id
            self.next_id += 1
            self.pending.add(rid)
        try:
            self.send({'id': rid, 'method': method, 'params': params or {}})
            end = time.monotonic() + timeout
            with self.ready:
                while rid not in self.responses:
                    if self.dead:
                        raise HostDisconnected('Authentication host disconnected')
                    left = end - time.monotonic()
                    if left <= 0:
                        raise TimeoutError('App-server RPC did not return: ' + method)
                    self.ready.wait(left)
                response = self.responses.pop(rid)
            if 'error' in response:
                # No provider body or credentials are echoed into diagnostics.
                raise HostRejected(method, response['error'])
            return response['result']
        finally:
            with self.ready:
                self.pending.discard(rid)

    def _respond(self, message):
        try:
            result = self.request(message['method'], message.get('params') or {})
            answer = {'id': message['id'], 'result': result}
        except BaseException:
            answer = {'id': message['id'], 'error': {'code': -32603, 'message': 'Task operation denied or incomplete'}}
        try:
            self.send(answer)
        except (OSError, HostDisconnected):
            pass

    def _read(self):
        try:
            for line in self.process.stdout:
                message = json.loads(line)
                if 'method' in message:
                    if 'id' in message:
                        self.executor.submit(self._respond, message)
                    else:
                        self.notification(message['method'], message.get('params') or {})
                elif 'id' in message:
                    with self.ready:
                        if message['id'] in self.pending:
                            self.responses[message['id']] = message
                            self.ready.notify_all()
        except BaseException:
            pass
        finally:
            with self.ready:
                self.dead = True
                self.ready.notify_all()
            self.notification('anc/hostDisconnected', {})

    def close(self):
        # Closing the host is a controller-lifetime operation, never task cancel.
        if self.process.poll() is None:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        self.executor.shutdown(wait=False, cancel_futures=True)
        # Popen does not close pipe objects after process exit. Release them on
        # the intentional host-lifetime close, including an already-dead host.
        self.process.stdin.close()
        self.process.stdout.close()
        if self.job_handle:
            import ctypes
            from ctypes import wintypes
            close = ctypes.WinDLL('kernel32').CloseHandle
            close.argtypes = [wintypes.HANDLE]
            close(self.job_handle)
            self.job_handle = None
        self.stderr.close()
