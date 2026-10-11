"""Shared official auth hosts; task capability, workspace and turn remain separate."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import tomllib
import uuid

from .appserver import AppServer, HostDisconnected
from .control import identifier, within, write_durable
from .instance import InstanceLock
from .mcp import TOOLS, handle as handle_mcp
from .policy import Denied
from .worker import launch as launch_cli


def capacity(value):
    if type(value) is not int or not 1 <= value <= 64:
        raise ValueError('Explicit integer capacity from 1 to 64 required')
    return value


def replace_durable(path, data):
    """Atomic mutable receipt. write_durable intentionally creates once only."""
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    write_durable(temporary, data)
    for attempt in range(40):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(.05)


@dataclass
class Run:
    job: dict
    token: str
    thread: str = None
    turn: str = None
    status: str = None
    interrupt_sent: bool = False
    done: threading.Event = field(default_factory=threading.Event)
    bound: threading.Event = field(default_factory=threading.Event)
    deadline: float = 0
    timed_out: bool = False
    start_sent: bool = False
    uncertain: bool = False
    usage: dict = None
    usage_written: bool = False


class Host:
    def __init__(self, pool, resource, scope, factory=AppServer):
        self.pool, self.resource, self.scope = pool, resource, scope
        self.runs, self.by_thread = {}, {}
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=capacity(resource['max_active']), thread_name_prefix='anc-turn')
        home = Path(scope['worker_home'])
        auth = home / 'CodexHome'
        self.lease = InstanceLock(auth / '.anc-auth-host.lock')
        self.lease.__enter__()
        # Startup is lazy: configuration/deployment do not issue a model request.
        self.client = None
        self.factory = factory
        self.host_dir = within(home, 'Hosts/' + resource['id'])
        self.limits = {}
        self.checks = ThreadPoolExecutor(max_workers=2, thread_name_prefix='anc-policy-check')

    def _start(self, job):
        if self.client:
            if self.client.dead:
                raise HostDisconnected('Host failed; runs are not retried')
            return
        home = Path(self.scope['worker_home'])
        self.host_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.host_dir / 'Temp'
        temporary.mkdir(exist_ok=True)
        env = {k: v for k, v in job['environment'].items() if not k.startswith('ANC_')}
        env.update(TEMP=str(temporary), TMP=str(temporary), CODEX_HOME=str(home / 'CodexHome'),
                   CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG='1')
        argv = [self.pool.runtime['codex'], 'app-server', '--stdio']
        for override in ('approval_policy="never"', 'features.hooks=false',
            'features.shell_tool=false', 'features.view_image=false', 'features.code_mode=false',
            'features.code_mode_only=false', 'features.apps=false', 'features.plugins=false',
            'features.multi_agent=false', 'web_search="disabled"', 'mcp_servers={}',
            'default_permissions="anc-host"',
            'permissions.anc-host={filesystem={":root"="deny",":minimal"="read"}}'):
            argv.extend(['-c', override])
        # Empty-table overrides can merge with user configuration. Disable each
        # inherited MCP explicitly so only task-bound dynamic tools are exposed.
        config_path = home / 'CodexHome' / 'config.toml'
        if config_path.is_file():
            with config_path.open('rb') as stream:
                servers = tomllib.load(stream).get('mcp_servers') or {}
            if not isinstance(servers, dict):
                raise Denied('Invalid inherited MCP configuration')
            for name in servers:
                if not isinstance(name, str):
                    raise Denied('Invalid MCP server name')
                argv.extend(['-c', 'mcp_servers.' + json.dumps(name) + '.enabled=false'])
        if self.pool.runtime.get('model_http_only'):
            argv.extend(['-c', 'model_provider="anc-openai-http"', '-c',
                'model_providers.anc-openai-http={name="OpenAI",wire_api="responses",requires_openai_auth=true,supports_websockets=false}'])
        if self.pool.runtime.get('model_catalog_json'):
            argv.extend(['-c', 'model_catalog_json=' + json.dumps(self.pool.runtime['model_catalog_json'])])
        self.client = self.factory(argv, env, str(self.host_dir), self.host_dir / 'stderr.txt',
                                   self.notification, self.request)
        # PID is diagnostic only, never used as task cancellation authority.
        replace_durable(self.host_dir / 'host.json', json.dumps({'pid': self.client.process.pid,
            'resource': self.resource['id'], 'started': time.time()}).encode())

    def launch(self, job):
        run = Run(job, job['environment']['ANC_TASK_TOKEN'], deadline=time.monotonic() + job['timeout_seconds'])
        with self.lock:
            if job['id'] in self.runs or len(self.runs) >= self.resource['max_active']:
                raise Denied('Host capacity or duplicate task')
            self.runs[job['id']] = run
        try:
            Path(job['workspace']).mkdir(parents=True, exist_ok=True)
            Path(job['environment']['TEMP']).mkdir(parents=True, exist_ok=True)
            Path(job['output_dir']).mkdir(parents=True, exist_ok=True)
            self.result(run, 'starting')
            self.executor.submit(self._execute, run)
        except BaseException:
            with self.lock:
                self.runs.pop(job['id'], None)
            raise

    def result(self, run, state, exit_code=None):
        if state in ('exited', 'failed', 'cancelled', 'timed_out') and run.usage and not run.usage_written:
            with (Path(run.job['output_dir']) / 'events.jsonl').open('a', encoding='utf-8') as out:
                out.write(json.dumps({'type': 'turn.completed', 'usage': run.usage}) + '\n')
            run.usage_written = True
        replace_durable(Path(run.job['output_dir']) / 'result.json', json.dumps({
            'job': run.job['id'], 'state': state, 'exit_code': exit_code,
            'backend': self.resource['id'], 'thread_id': run.thread,
            'turn_id': run.turn, 'observed_at': time.time()}).encode())

    def _execute(self, run):
        try:
            api = self.pool.api
            ctx = api.context(run.token)
            if self.pool.cancel_path(run.job).exists():
                raise Denied('Task cancelled before start')
            if not api.call(run.token, 'preflight', {}).get('complete'):
                raise Denied('Full rules preflight incomplete')
            rules = api.rules.load(ctx, 'UserPromptSubmit')
            images = getattr(api, 'generated_images', None)
            if images:
                try:
                    images.app(ctx)
                except Denied:
                    pass
                else:
                    from .generated_images import INSTRUCTIONS
                    rules += '\n' + INSTRUCTIONS
            # Serialize only host creation. RPCs and model turns remain concurrent.
            with self.start_lock:
                self._start(run.job)
            filesystem = {':root': 'deny', ':minimal': 'read', run.job['workspace']: 'write',
                run.job['environment']['TEMP']: 'write',
                str(Path(self.pool.runtime['python']).resolve().parent): 'read',
                str(Path(self.pool.runtime['framework']).resolve()): 'read',
                str(Path(self.scope['worker_home']) / 'Work' / '.agents'): 'read',
                str(Path(self.scope['worker_home']) / 'CodexHome'): 'deny'}
            config = {'default_permissions': 'anc-task',
                'permissions': {'anc-task': {'filesystem': filesystem}},
                'shell_environment_policy': {'inherit': 'none', 'set': {
                    'TEMP': run.job['environment']['TEMP'], 'TMP': run.job['environment']['TMP']}},
                'features': {'hooks': False, 'shell_tool': False, 'apps': False, 'plugins': False,
                             'multi_agent': False, 'code_mode': False, 'view_image': False},
                'mcp_servers': {}, 'web_search': 'disabled'}
            tools = [{'type': 'function', 'name': 'anc_' + t['name'],
                'description': t['description'], 'inputSchema': t['inputSchema']} for t in TOOLS]
            # Files and commands are mediated by the existing task-bound ANC
            # tools (Commands uses its own per-command AppContainer). An empty
            # official environment selection removes native filesystem/shell
            # runtimes entirely, including instruction-file loading and Windows
            # sandbox provisioning. Keep the restrictive profile as defence in
            # depth; never replace it with externalSandbox/full access.
            started = self.client.rpc('thread/start', {'ephemeral': True, 'environments': [],
                'cwd': run.job['workspace'], 'allowProviderModelFallback': False,
                'permissions': 'anc-task', 'approvalPolicy': 'never', 'config': config,
                'model': run.job['model'], 'developerInstructions': rules +
                '\n本宿主通过官方动态工具接口提供 ANC：实际函数名是 anc_ 加短名，如 anc_read、anc_read_many、anc_save_work、anc_reply。'
                '旧说明中的 anc.read 或 mcp__anc__read 在本宿主调用 anc_read；其余短名同样映射。参数与规则不变。'
                '本对话没有原生执行环境；文件和命令操作通过本任务授权的 ANC 工具完成，命令使用 anc_execute。',
                'dynamicTools': tools})
            if started['model'] != run.job['model']:
                raise Denied('Requested model was not preserved')
            with self.lock:
                run.thread = started['thread']['id']
                self.by_thread[run.thread] = run
            api.rules.load(api.context(run.token), 'SessionStart', run.thread)
            if self.pool.cancel_path(run.job).exists():
                raise Denied('Task cancelled before turn')
            run.start_sent = True
            turn = self.client.rpc('turn/start', {'threadId': run.thread, 'environments': [],
                'input': [{'type': 'text', 'text': run.job['prompt']}],
                'model': run.job['model'], 'effort': run.job['reasoning'],
                'cwd': run.job['workspace'], 'approvalPolicy': 'never'})['turn']
            with self.lock:
                if run.turn and run.turn != turn['id']:
                    raise Denied('Host turn binding changed')
                run.turn = turn['id']
                run.bound.set()
            self.result(run, 'running')
            while not run.done.wait(.1):
                self.check(run)
            state = ('timed_out' if run.timed_out else 'cancelled') if run.status == 'interrupted' else (
                'exited' if run.status == 'completed' else 'failed')
            self.result(run, state, 0 if state == 'exited' else 1)
        except BaseException as error:
            write_durable(Path(run.job['output_dir']) / 'failure.json', json.dumps({
                'job': run.job['id'], 'error_type': type(error).__name__,
                'protocol_code': getattr(error, 'protocol_error', {}).get('code')}).encode())
            # An uncertain start still owns the slot until host termination or
            # an exact interrupted-turn receipt. Never retry the user work.
            if run.start_sent and self.client and not self.client.dead and not run.done.is_set():
                if run.turn:
                    try:
                        self.client.rpc('turn/interrupt', {'threadId': run.thread, 'turnId': run.turn})
                        run.done.wait(10)
                    except BaseException:
                        pass
                if not run.done.is_set():
                    run.uncertain = True
                    self.result(run, 'uncertain')
                    return
            cancelled = self.pool.cancel_path(run.job).exists()
            run.done.set()
            self.result(run, 'cancelled' if cancelled else 'failed', 1)
        finally:
            if run.done.is_set() or not run.thread or (self.client and self.client.dead):
                with self.lock:
                    self.runs.pop(run.job['id'], None)
                    self.by_thread.pop(run.thread, None)
                if run.thread and self.client and not self.client.dead:
                    try:
                        self.client.rpc('thread/unsubscribe', {'threadId': run.thread}, timeout=5)
                    except BaseException:
                        pass

    def check(self, run):
        cancel = self.pool.cancel_path(run.job).exists()
        expired = time.monotonic() >= run.deadline
        if (cancel or expired) and run.turn and not run.interrupt_sent:
            self.pool.api.revoke(run.token)
            run.timed_out = expired and not cancel
            self.client.rpc('turn/interrupt', {'threadId': run.thread, 'turnId': run.turn})
            run.interrupt_sent = True

    def retain_image_notification(self, method, params):
        item = params.get('item') or {}
        if method != 'item/completed' or item.get('type') != 'imageGeneration':
            return
        images = getattr(self.pool.api, 'generated_images', None)
        if images is None:
            return
        with self.lock:
            run = self.by_thread.get(params.get('threadId'))
            if not run or (params.get('threadId'), params.get('turnId')) != (run.thread, run.turn) or not run.turn or run.done.is_set():
                return
        # Scheduler launch takes control.lock then host.lock. Never acquire the
        # protected writer while holding host.lock in the notification reader.
        try:
            images.capture(self.pool.api.context(run.token), run.thread, run.turn, item)
        except Exception as error:
            with self.pool.api.c.lock, self.pool.api.c.db.transaction() as db:
                self.pool.api.c.audit(db, 'native-image', run.job['id'], 'image.retention_failed', {'error_type':type(error).__name__})

    def notification(self, method, params):
        self.retain_image_notification(method, params)
        with self.lock:
            if method == 'account/rateLimits/updated':
                incoming = params.get('rateLimits') or {}
                for name in ('primary', 'secondary'):
                    window = incoming.get(name)
                    if isinstance(window, dict) and type(window.get('usedPercent')) is int:
                        self.limits[name] = {'used_percent': window['usedPercent'],
                            'window_minutes': window.get('windowDurationMins'), 'resets_at': window.get('resetsAt')}
                if isinstance(incoming.get('credits'), dict):
                    self.limits['credits'] = {'has_credits': incoming['credits'].get('hasCredits') is True}
                replace_durable(Path(self.scope['worker_home']) / 'CodexHome' / 'anc-rate-limits.json',
                    json.dumps({'rate_limits': self.limits, 'timestamp': datetime.now(timezone.utc).isoformat()}).encode())
                return
            if method == 'anc/hostDisconnected':
                for run in self.runs.values():
                    self.pool.api.revoke(run.token)
                    run.status = 'failed'
                    run.bound.set()
                    run.done.set()
                return
            run = self.by_thread.get(params.get('threadId'))
            if not run:
                return
            turn = params.get('turn') or {}
            if method == 'turn/started' and not run.turn:
                run.turn = turn.get('id')
                run.bound.set()
            # Ignore other turns even if the thread matches.
            event_turn = params.get('turnId') or turn.get('id')
            if event_turn and run.turn and event_turn != run.turn:
                return
            logged = params
            image_item = params.get('item') or {}
            if image_item.get('type') == 'imageGeneration':
                if (params.get('threadId'), params.get('turnId')) != (run.thread, run.turn) or not run.turn or run.done.is_set():
                    return
                # Native bytes are retained once, never duplicated in generic logs.
                logged = dict(params, item={k:v for k,v in image_item.items() if k != 'result'})
            with (Path(run.job['output_dir']) / 'events.jsonl').open('a', encoding='utf-8') as out:
                out.write(json.dumps({'method': method, 'params': logged}, ensure_ascii=False) + '\n')
            if method == 'turn/completed' and run.turn == turn.get('id'):
                run.status = turn.get('status')
                run.done.set()
            if method == 'thread/tokenUsage/updated' and event_turn == run.turn:
                # Each thread is new for exactly this task, so total is its
                # cumulative usage across tool/model exchanges, saved once.
                last = (params.get('tokenUsage') or {}).get('total') or {}
                run.usage = {'input_tokens': last.get('inputTokens', 0),
                    'cached_input_tokens': last.get('cachedInputTokens', 0),
                    'output_tokens': last.get('outputTokens', 0),
                    'reasoning_output_tokens': last.get('reasoningOutputTokens', 0)}
            if method == 'item/completed':
                item = params.get('item') or {}
                if item.get('type') == 'dynamicToolCall':
                    with (Path(run.job['output_dir']) / 'events.jsonl').open('a', encoding='utf-8') as out:
                        out.write(json.dumps({'type': 'item.completed', 'item': {'type': 'tool_call', 'id': item.get('id')}}) + '\n')
                if item.get('type') == 'contextCompaction':
                    self.checks.submit(self.after_compact, run)
            if method == 'thread/compacted':
                self.checks.submit(self.after_compact, run)

    def after_compact(self, run):
        if run.done.is_set():
            return
        try:
            self.pool.api.rules.load(self.pool.api.context(run.token), 'PostCompact', run.thread)
        except BaseException:
            self.pool.api.revoke(run.token)
            cancel = self.pool.cancel_path(run.job)
            try:
                write_durable(cancel, b'full rules revalidation failed\n')
            except FileExistsError:
                pass

    def request(self, method, params):
        if method != 'item/tool/call':
            raise Denied('Native approval/tool request unavailable')
        with self.lock:
            run = self.by_thread.get(params.get('threadId'))
        if not run or not run.bound.wait(5) or params.get('turnId') != run.turn or run.done.is_set():
            raise Denied('Task/thread/turn capability binding unavailable')
        if self.pool.cancel_path(run.job).exists():
            self.pool.api.revoke(run.token)
            raise Denied('Task cancelled')
        tool = params.get('tool')
        allowed = {'anc_' + t['name']: t['name'] for t in TOOLS}
        if tool not in allowed or params.get('namespace'):
            raise Denied('Task tool unavailable')
        args = params.get('arguments')
        if not isinstance(args, dict):
            raise Denied('Invalid tool arguments')
        ctx = self.pool.api.context(run.token)
        self.pool.api.rules.load(ctx, 'PreToolUse', run.thread)
        result = handle_mcp({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': allowed[tool], 'arguments': args}},
            lambda name, arguments: self.pool.api.call(run.token, name, arguments))['result']
        items = []
        for content in result['content']:
            if content['type'] == 'text':
                items.append({'type': 'inputText', 'text': content['text']})
            elif content['type'] == 'image':
                items.append({'type': 'inputImage', 'imageUrl': 'data:' + content['mimeType'] + ';base64,' + content['data']})
        return {'success': not result.get('isError', False), 'contentItems': items}

    def close(self):
        with self.lock:
            for run in self.runs.values():
                self.pool.api.revoke(run.token)
            client = self.client
        if client:
            client.close()
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.checks.shutdown(wait=True, cancel_futures=True)
        self.lease.__exit__()


class BackendPool:
    def __init__(self, api, runtime, scopes, config, host_factory=Host):
        self.api, self.runtime, self.scopes = api, runtime, scopes
        self.resources, self.hosts, self.host_factory = {}, {}, host_factory
        self.bot_limits = config.get('bots') or {}
        for app, limit in self.bot_limits.items():
            capacity(limit)
        paths = set()
        for rid, item in (config.get('resources') or {}).items():
            identifier(rid)
            if item.get('type') != 'codex-app-server' or item.get('worker_scope') not in scopes:
                raise ValueError('Explicit supported backend and configured worker scope required')
            scope = scopes[item['worker_scope']]
            path = str((Path(scope['worker_home']) / 'CodexHome').resolve()).casefold()
            if path in paths:
                raise ValueError('One resource per canonical auth home required')
            paths.add(path)
            capacity(item['max_active'])
            self.resources[rid] = dict(item, id=rid)
        self.lock = threading.Lock()

    def resource(self, scope):
        rid = scope.get('backend')
        if rid is None:
            return None
        if rid not in self.resources:
            raise Denied('Configured backend unavailable')
        declared = self.scopes[self.resources[rid]['worker_scope']]
        if (str(Path(scope['worker_home']).resolve()).casefold() != str(Path(declared['worker_home']).resolve()).casefold()
                or scope['os_account'] != declared['os_account']):
            raise Denied('Backend/auth home binding mismatch')
        return rid

    def cancel_path(self, job):
        return Path(job['job_path']).with_suffix('.cancel')

    def launch(self, job, runtime):
        rid = job.get('backend')
        if not rid:
            return launch_cli(job, runtime)
        # Persist once before submitting. A process failure cannot justify replay.
        write_durable(Path(job['job_path']), json.dumps(job, ensure_ascii=False).encode())
        with self.lock:
            if rid not in self.hosts:
                item = self.resources[rid]
                self.hosts[rid] = self.host_factory(self, item, self.scopes[item['worker_scope']])
            host = self.hosts[rid]
        host.launch(job)

    def close(self):
        for host in self.hosts.values():
            host.close()

    def poll(self):
        for host in self.hosts.values():
            with host.lock:
                uncertain = [run for run in host.runs.values() if run.uncertain]
            for run in uncertain:
                try:
                    host.check(run)
                except (OSError, RuntimeError, TimeoutError):
                    pass
                if run.done.is_set():
                    host.result(run, 'cancelled' if run.status == 'interrupted' else 'failed', 1)
                    self.api.revoke(run.token)
                    with host.lock:
                        host.runs.pop(run.job['id'], None)
                        host.by_thread.pop(run.thread, None)

    def recover(self, row):
        """Confirm the previous contained host is gone before releasing an old slot.

        A PID still alive (including reuse) fails closed. No old job is submitted.
        Controller death closes its Windows job, so the ordinary path is gone.
        """
        try:
            job = json.loads(Path(row['job_path']).read_text(encoding='utf-8-sig'))
            rid = job.get('backend')
            if rid not in self.resources or job['id'] != row['task'] or job['output_dir'] != row['output_dir']:
                return False
            scope = self.scopes[self.resources[rid]['worker_scope']]
            state_path = Path(scope['worker_home']) / 'Hosts' / rid / 'host.json'
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding='utf-8'))
                if type(state.get('pid')) is not int or state.get('resource') != rid:
                    return False
                if os.name == 'nt':
                    import ctypes
                    from ctypes import wintypes as w
                    k = ctypes.WinDLL('kernel32', use_last_error=True)
                    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
                    k.OpenProcess.restype = w.HANDLE
                    k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
                    k.CloseHandle.argtypes = [w.HANDLE]
                    handle = k.OpenProcess(0x1000, False, state['pid'])
                    if not handle:
                        if ctypes.get_last_error() != 87:
                            return False
                    else:
                        code = w.DWORD()
                        try:
                            if not k.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259:
                                return False
                        finally:
                            k.CloseHandle(handle)
                else:
                    try:
                        os.kill(state['pid'], 0)
                        return False
                    except ProcessLookupError:
                        pass
            replace_durable(Path(row['output_dir']) / 'result.json', json.dumps({
                'job': row['task'], 'state': 'failed', 'exit_code': 1,
                'backend': rid, 'recovered_without_retry': True}).encode())
            return True
        except (OSError, ValueError, KeyError):
            return False
