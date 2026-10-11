"""Protected Windows collector process with a live parent guard and job cleanup."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from .control import digest, within, write_durable
from .policy import Denied
from .wechat import MAX_SOURCE_BYTES


ADAPTER_HASHES = {
    'capture_owner.py': '5dbf85502aa27733e1267c794335835ad7823c32b29bb14cfdf8e25609f40f0b',
    'capture_wechat.py': 'a0145263e7d904198f3342ae0167e8021e1a85f436cb76b50b825086a4507a16',
    'capture_active_owner.py': '59caaa7a105754098badf366e3bcfaa43f24bc4113046f3a7efe3709aa967370',
}


def checked_path(value):
    path = Path(value)
    if not path.is_absolute():
        raise Denied('Absolute protected deployment path required')
    within(path.parent, path.name)
    return path


def verify_adapter(root):
    root = checked_path(root)
    for name, expected in ADAPTER_HASHES.items():
        path = within(root, name)
        if digest(path.read_bytes()) != expected:
            raise Denied('Unreviewed WeChat adapter version')
    return root


class ProtectedCollector:
    def __init__(self, control_root, framework, config):
        self.root = checked_path(control_root)
        self.framework, self.config = checked_path(framework), config
        if self.root.drive.casefold() == 'c:':
            raise Denied('Fresh capture requires protected non-C storage')

    def __call__(self, grant, start, end, run_id, guard):
        return self._run(grant, start, end, run_id, guard)

    def profile(self, account_id, wxid, run_id, guard):
        grant={'account_id':account_id,'wxid':wxid,'target':wxid}
        result=self._run(grant,0,1,'profile-'+run_id,guard,profile=True)
        return json.loads(result['raw'])

    def attachment(self, wxid, message, run_id, guard, frame_times=None):
        accounts = [(key, item) for key, item in self.config['accounts'].items()
                    if item.get('enabled') is True and item.get('wxid') == wxid]
        if len(accounts) != 1:
            raise Denied('Original attachment account is not provisioned uniquely')
        grant = {'account_id': accounts[0][0], 'wxid': wxid, 'target': message['chat']}
        result = self._run(grant, message['create_time'], message['create_time']+1,
                           'attachment-'+run_id, guard, message,frame_times=frame_times)
        body=json.loads(result['raw'])
        if 'original' in result:body['_original_bytes']=result['original']
        return body

    def _run(self, grant, start, end, run_id, guard, attachment=None, profile=False, frame_times=None):
        from . import appcontainer as win
        from .commands import system_environment
        runtime = self.config
        if not runtime.get('deployment_approval'):
            raise Denied('Explicit collector deployment approval required')
        account = runtime['accounts'].get(grant['account_id'])
        if not account or account.get('enabled') is not True or account['wxid'] != grant['wxid']:
            raise Denied('Capture account is not provisioned')
        verify_adapter(runtime['adapter_dir'])
        python = checked_path(runtime['python'])
        run = within(self.root, 'WeChatRuntime/runs/' + run_id)
        run.mkdir(parents=True, exist_ok=False)
        private = within(self.root, 'WeChatRuntime')
        cache = within(private, 'cache/' + grant['account_id'])
        cache.mkdir(parents=True, exist_ok=True)
        temporary = within(run, 'temp'); temporary.mkdir()
        job_body = {'adapter_dir': runtime['adapter_dir'], 'upstream_root': runtime['upstream_root'],
                    'account': account, 'target': grant['target'], 'start': start, 'end': end,
                    'private_root': str(private), 'cache': str(cache), 'run': str(run)}
        if grant.get('selector'):
            job_body['selector'] = grant['selector']
        if attachment is not None:
            job_body['attachment'] = attachment
            job_body['media_converter'] = runtime.get('media_converter', {})
            if frame_times is not None:job_body['frame_times']=frame_times
        if profile:
            job_body['profile_only']=True
        env = system_environment(os.environ)
        env.update(TEMP=str(temporary), TMP=str(temporary), USERPROFILE=str(temporary),
                   PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
        events = queue.Queue(maxsize=16)
        job = win.check(win.create_job(None, None), 'create capture job')
        proc, reader, assigned = None, None, False
        try:
            limits = win.ExtendedLimits()
            limits.basic.flags = 0x2000 | 0x200 | 0x8
            limits.basic.active_process_limit = 8
            limits.job_memory = 512 * 1024 * 1024
            win.check(win.set_job(job, 9, win.C.byref(limits), win.C.sizeof(limits)), 'bound capture process')
            guard()
            # The child blocks on stdin before importing a collector or reading
            # account data. Assign the whole process tree before providing work.
            proc = subprocess.Popen([str(python), '-B', '-X', 'utf8',
                str(within(self.framework, 'tools/wechat_capture_entry.py'))],
                cwd=run, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                creationflags=subprocess.CREATE_NO_WINDOW)
            win.check(win.assign_job(job, int(proc._handle)), 'assign capture descendants')
            assigned = True

            def read_protocol():
                try:
                    while True:
                        line = proc.stdout.readline(4097)
                        if not line:
                            break
                        if len(line) > 4096:
                            raise ValueError('Collector protocol overflow')
                        events.put(json.loads(line), timeout=1)
                except Exception:
                    events.put({'type': 'error'}, timeout=1)
                finally:
                    try: events.put({'type': 'eof'}, timeout=1)
                    except queue.Full: pass

            reader = threading.Thread(target=read_protocol, daemon=True)
            reader.start()
            proc.stdin.write(json.dumps(job_body)+'\n'); proc.stdin.flush()
            deadline, complete = time.monotonic()+180, False
            while True:
                guard()
                if time.monotonic() >= deadline:
                    raise Denied('Capture deadline exceeded; no result published')
                try: event = events.get(timeout=.5)
                except queue.Empty: continue
                if event.get('type') == 'guard':
                    guard()
                    proc.stdin.write('{"allowed":true}\n'); proc.stdin.flush()
                elif event.get('type') == 'complete':
                    complete = True
                elif event.get('type') == 'eof':
                    break
                else:
                    raise Denied('Protected collector failed; no partial original published')
            if proc.wait(timeout=5) != 0 or not complete:
                raise Denied('Capture completion is unconfirmed')
            guard()
            export = within(run, 'export')
            with within(export, 'messages.json').open('rb') as stream:
                raw = stream.read(MAX_SOURCE_BYTES+1)
            receipt_path = within(export, 'receipt.json')
            with receipt_path.open('rb') as stream:
                receipt_bytes = stream.read(1024*1024+1)
            if len(raw) > MAX_SOURCE_BYTES or len(receipt_bytes) > 1024*1024:
                raise Denied('Capture output budget exceeded')
            returned={'raw': raw, 'receipt': json.loads(receipt_bytes)}
            if attachment is not None:
                meta=json.loads(raw).get('retained_original')
                if meta:
                    with within(export,'original.bin').open('rb') as stream:original=stream.read(32*1024*1024+1)
                    if len(original)>32*1024*1024 or len(original)!=meta['bytes'] or digest(original)!=meta['sha256']:
                        raise Denied('Acquired original receipt mismatch')
                    returned['original']=original
            return returned
        finally:
            cleaned = False
            try:
                if proc and not assigned and proc.poll() is None:
                    proc.kill()  # Assignment failure leaves only the blocked entry.
                win.check(win.terminate_job(job, 1), 'stop capture descendants')
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ActiveProcesses
                    accounting = (win.C.c_byte * 48)()
                    win.check(win.query_job(job, 1, accounting, 48, None), 'verify capture cleanup')
                    if win.W.DWORD.from_buffer(accounting,40).value == 0:
                        cleaned = True
                        break
                    time.sleep(.05)
                if proc:
                    proc.wait(timeout=10)
            finally:
                win.close(job)  # Kernel kills descendants if controller exits too.
                if proc:
                    for stream in (proc.stdin, proc.stdout):
                        if stream: stream.close()
                if reader: reader.join(timeout=2)
                write_durable(within(run, 'process-stopped.json'), json.dumps({
                    'process_exited': proc is None or proc.poll() is not None,
                    'descendants_stopped': cleaned, 'job_closed': True}).encode())
            if not cleaned:
                raise Denied('Capture descendant cleanup could not be confirmed')
