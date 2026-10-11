"""Task-scoped offline commands; no model loop or semantic decisions."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from .control import within, write_durable
from .policy import Denied
from .worker import launch, reap_command


def validate(command, timeout):
    if not isinstance(command, str) or not command.strip() or len(command) > 20000 or '\0' in command:
        raise ValueError('Command must contain 1–20000 characters and no NUL')
    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError('Timeout must be 1–120 seconds')


def system_environment(source):
    values = {key.upper():value for key,value in source.items()}
    allowed = ('SystemRoot','WINDIR','ALLUSERSPROFILE','ProgramData','ProgramFiles',
               'ProgramFiles(x86)','CommonProgramFiles','CommonProgramFiles(x86)',
               'COMSPEC','SystemDrive','USERNAME','USERDOMAIN','HOMEDRIVE','HOMEPATH')
    result = {key:values[key.upper()] for key in allowed if key.upper() in values}
    if 'SystemRoot' not in result:
        raise RuntimeError('Windows system location unavailable')
    # PowerShell otherwise treats even an explicit .exe path as a document.
    result['PATHEXT'] = '.COM;.EXE;.BAT;.CMD'
    return result


def no_links(path):
    for node in (Path(path), *Path(path).parents):
        if node.exists() and node.lstat().st_file_attributes & 0x400:
            raise Denied('Linked execution path refused')


def no_file_aliases(root):
    """Do not let inherited ACL updates reach a junction or hardlinked file.

    Invoked only while no command for this task is running. Task roots cannot
    be renamed by a container, and a prior job must have confirmed termination.
    """
    no_links(root)
    count = 0
    pending = [Path(root)]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        count += 1
        if count > 50000:
            raise Denied('Scratch directory exceeds safe ACL inspection bound')
        if getattr(info, 'st_file_attributes', 0) & 0x400 or path.is_symlink():
            raise Denied('Reparse points are not allowed in command scratch')
        if path.is_dir():
            pending.extend(path.iterdir())
        elif info.st_nlink != 1:
            raise Denied('Hardlinked scratch files require protected cleanup')


def acl(path, *args):
    no_links(path)
    result = subprocess.run(['icacls.exe', str(path), *args], capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Command directory ACL operation failed')


def save_journal(path, body):
    temporary = path.with_suffix('.pending')
    with temporary.open('wb') as stream:
        stream.write(json.dumps(body).encode())
        stream.flush()
        os.fsync(stream.fileno())
    # A status reader on Windows can briefly hold the destination without
    # FILE_SHARE_DELETE. Retry only the atomic rename, never the command.
    deadline = time.monotonic()+2
    while True:
        try:
            temporary.replace(path)
            break
        except OSError as error:
            if getattr(error, 'winerror', None) not in (5,32,33) or time.monotonic() >= deadline:
                raise
            time.sleep(.025)


class Commands:
    def __init__(self, control, runtime, scopes):
        self.c, self.runtime, self.scopes = control, runtime, scopes
        self.active, self.lock = set(), threading.Lock()
        self.journals = control.root/'CommandRuns'
        self.journals.mkdir(exist_ok=True)
        self.recovery = {}
        for path in self.journals.glob('*.json'):
            item = json.loads(path.read_text())
            if item['state'] != 'cleaned':
                self.recovery[item['task']] = path
                if item['state'] == 'launching' and not Path(item['cancel']).exists():
                    write_durable(Path(item['cancel']),b'controller recovery requested termination')

    def execute(self, ctx, command, timeout=60, *, files=None):
        validate(command, timeout)
        if files is not None and (not isinstance(files, dict) or len(files) > 200
                or any(not isinstance(v, bytes) for v in files.values())
                or sum(len(v) for v in files.values()) > 32*1024*1024):
            raise ValueError('Bounded controller-supplied execution files required')
        with self.lock:
            if ctx.task in self.recovery:
                raise Denied('Earlier command cleanup requires reconciliation before continuing this task')
            if ctx.task in self.active or len(self.active) + len(self.recovery) >= 2:
                raise Denied('An execution slot is already in use; wait for its receipt')
            self.active.add(ctx.task)
        try:
            return self._run(ctx, command, timeout, files)
        finally:
            with self.lock:
                self.active.discard(ctx.task)

    def _current(self, ctx):
        with self.c.db.transaction() as db:
            self.c._check(db, ctx)

    def _run(self, ctx, command, timeout, files=None):
        from .appcontainer import identity
        self._current(ctx)
        from .runtime_scopes import resolve
        scope, runtime = resolve(self.scopes, self.c, ctx), self.runtime
        home = Path(scope['worker_home'])
        workspace = within(home, 'Work/tasks/'+ctx.task)
        temporary = within(home, 'Temp/tasks/'+ctx.task)
        run_id = 'cmd-'+uuid.uuid4().hex
        name = 'ANC.Command.'+run_id
        sid = identity(name)
        output = within(home, 'Outputs/'+run_id)
        jobs = Path(scope['jobs'])
        cancel = jobs/(run_id+'.cancel')
        receipt = output/'command-result.json'
        grants, launched, cleaned = [], False, False
        result = None
        journal_path = self.journals/(run_id+'.json')
        journal = {'id':run_id,'task':ctx.task,'sid':sid,'cancel':str(cancel),
                   'receipt':str(receipt),'state':'preparing','grants':[],
                   'scratch':[str(workspace),str(temporary)]}
        journal['headless'] = bool(runtime.get('headless'))
        save_journal(journal_path,journal)

        def grant(path, permission):
            # Persist cleanup intent before changing any ACL.
            grants.append(path)
            journal['grants'] = [str(p) for p in grants]
            save_journal(journal_path,journal)
            acl(path,'/grant','*'+sid+':'+permission)
        try:
            read_roots = (Path(runtime['python']).parent, Path(runtime['framework']))
            for path in (*read_roots, workspace, temporary, output):
                no_links(path)
                path.mkdir(parents=True, exist_ok=True)
            for path in (workspace,temporary):
                no_file_aliases(path)
            # Protected callers may stage a verified project into this one task.
            # The RPC execute operation does not expose this argument. No writes
            # race an active job; prior command cleanup is checked by execute.
            for relative, raw in (files or {}).items():
                path = within(workspace, relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                no_links(path)
                with path.open('xb') as stream:
                    stream.write(raw)
            for path, mode in ((*[(p,'RX') for p in read_roots], (workspace,'M'), (temporary,'M'))):
                grant(path,'(OI)(CI)'+mode)
                if mode == 'M':
                    acl(path, '/setowner', '*S-1-5-32-544')
                    acl(path, '/setintegritylevel', '(OI)(CI)L')
            ancestors = set()
            for path in (*read_roots,workspace,temporary):
                ancestors.update(path.parents)
            for path in sorted(ancestors,key=lambda p:len(p.parts)):
                grant(path,'(X)')
            env = system_environment(os.environ)
            env.update(PATH=str(read_roots[0])+r';C:\Windows\System32', TEMP=str(temporary), TMP=str(temporary),
                       USERPROFILE=str(workspace), APPDATA=str(temporary/'AppData/Roaming'),
                       LOCALAPPDATA=str(temporary/'AppData/Local'), PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            for key in ('APPDATA','LOCALAPPDATA'):
                Path(env[key]).mkdir(parents=True,exist_ok=True)
            location = str(workspace).replace("'", "''")
            script = ("$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
                      "New-PSDrive -Name ANCWork -PSProvider FileSystem -Root '"+location+
                      "' -Scope Script -ErrorAction Stop | Out-Null; Set-Location -LiteralPath 'ANCWork:\\'; "
                      "[Environment]::CurrentDirectory=(Get-PSDrive ANCWork).Root; "+command)
            executable = str(Path(env['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
            spec = {'id':run_id,'name':name,'argv':[executable,'-NoProfile','-NonInteractive','-Command',script],
                    'cwd':str(workspace),'environment':env,'timeout':timeout,'cancel':str(cancel),
                    'receipt':str(receipt),'max_output':1024*1024}
            if runtime.get('headless'):
                spec['desktop_name'] = name+'\\Default'
            spec_path = within(jobs,run_id+'-container.json')
            write_durable(spec_path,json.dumps(spec,ensure_ascii=False).encode())
            host_env = dict(env, CODEX_HOME=str(home/'CodexHome'))
            job = {'id':run_id,'os_account':scope['os_account'],'workspace':str(workspace),
                   'output_dir':str(output),'environment':host_env,'require_anc_preflight':False,
                   'argv':[runtime['python'],'-B','-X','utf8',str(Path(runtime['framework'])/'tools/command_entry.py'),str(spec_path)],
                   'timeout_seconds':timeout+30,'prompt':''}
            if runtime.get('headless'):
                job['desktop_container'] = {'name':name,'sid':sid}
            self._current(ctx)
            with self.c.db.transaction() as db:
                self.c.audit(db,ctx.principal,ctx.task,'command.requested',{'id':run_id,'timeout':timeout})
            launched = True  # A launcher timeout may still have started the worker.
            journal['state'] = 'launching'
            save_journal(journal_path,journal)
            try:
                launch(job,dict(runtime,jobs=str(jobs)))
            except Exception:
                write_durable(cancel,b'launch status uncertain')
            deadline = time.monotonic()+timeout+50
            while time.monotonic() < deadline:
                try:
                    self._current(ctx)
                except Exception:
                    if not cancel.exists():
                        write_durable(cancel,b'current authority unavailable')
                if receipt.exists():
                    result = json.loads(receipt.read_text(encoding='utf-8'))
                    if result.get('id') != run_id:
                        raise RuntimeError('Command receipt identity mismatch')
                    cleaned = result.get('cleanup_confirmed') is True
                    break
                time.sleep(0.2)
            if result is None:
                if not cancel.exists():
                    write_durable(cancel,b'command completion unconfirmed')
                raise RuntimeError('Command completion is unconfirmed; inspect protected execution receipt')
            if not cleaned:
                raise RuntimeError('Command cleanup is unconfirmed; inspect protected execution receipt')
            self._current(ctx)
            with self.c.db.transaction() as db:
                self.c.audit(db,ctx.principal,ctx.task,'command.finished',
                             {'id':run_id,'state':result['state'],'exit_code':result.get('exit_code'),
                              'output_truncated':result['output_truncated'],'cleanup_confirmed':True})
            return {k:result[k] for k in ('id','state','stdout','stderr','output_truncated','cleanup_confirmed')} | {'exit_code':result.get('exit_code')}
        finally:
            if not launched or cleaned:
                try:
                    reap_command(run_id,runtime)
                except Exception:
                    self.recovery[ctx.task] = journal_path
                    raise
                failures = []
                for path in (workspace,temporary):
                    try:
                        no_file_aliases(path)
                    except Exception:
                        self.recovery[ctx.task] = journal_path
                        journal['state'] = 'requires_file_review'
                        save_journal(journal_path,journal)
                        raise
                for path in reversed(list(dict.fromkeys(grants))):
                    try:
                        acl(path,'/remove:g','*'+sid)
                    except Exception:
                        failures.append(str(path))
                if failures:
                    self.recovery[ctx.task] = journal_path
                    raise RuntimeError('Container ACL cleanup incomplete; inspect command '+run_id)
                journal['state'] = 'cleaned'
                save_journal(journal_path,journal)
            else:
                self.recovery[ctx.task] = journal_path

    def reconcile(self):
        """Recover exact prior attempts without starting a duplicate command."""
        with self.lock:
            for task,path in list(self.recovery.items()):
                item = json.loads(path.read_text())
                if item['state'] == 'requires_file_review':
                    continue
                receipt = Path(item['receipt'])
                if item['state'] == 'launching':
                    if not receipt.exists():
                        continue
                    result = json.loads(receipt.read_text(encoding='utf-8'))
                    if result.get('id') != item['id'] or not result.get('cleanup_confirmed'):
                        continue
                for scratch in item.get('scratch',[]):
                    no_file_aliases(Path(scratch))
                reap_command(item['id'],dict(self.runtime,headless=item.get('headless',False)))
                for target in reversed(list(dict.fromkeys(item['grants']))):
                    acl(Path(target),'/remove:g','*'+item['sid'])
                item['state'] = 'cleaned'
                save_journal(path,item)
                del self.recovery[task]
