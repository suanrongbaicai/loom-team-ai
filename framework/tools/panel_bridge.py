"""Local operator panel helper. No network listener or model-facing API.

Job limits prevent accidental runaway maintenance commands; they are NOT an
authorization sandbox for untrusted code. Existing Bot isolation is unchanged.
"""
import base64
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def bounded(argv, cwd, timeout=60, max_output=65536, memory=512*1024*1024):
    from anc import appcontainer as win
    job = win.check(win.create_job(None, None), 'create maintenance job')
    proc = None
    chunks, total, overflow = [], [0], threading.Event()
    try:
        limits = win.ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x200 | 0x8
        limits.basic.active_process_limit = 12
        limits.job_memory = memory
        win.check(win.set_job(job, 9, win.C.byref(limits), win.C.sizeof(limits)), 'bound maintenance job')
        # This fixed child waits before executing the requested command.
        proc = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', __file__, '--child'],
            cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        win.check(win.assign_job(job, int(proc._handle)), 'assign maintenance child')
        def drain():
            while True:
                part = proc.stdout.read1(4096)
                if not part:
                    return
                keep = max(0, max_output-total[0])
                chunks.append(part[:keep]) if keep else None
                total[0] += len(part)
                if total[0] > max_output:
                    overflow.set()
                    return
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        proc.stdin.write((json.dumps(argv)+'\n').encode())
        proc.stdin.close()
        start, reason = time.monotonic(), 'finished'
        while proc.poll() is None:
            if overflow.is_set():
                reason = 'output_limit'; break
            if time.monotonic()-start > timeout:
                reason = 'timeout'; break
            time.sleep(.05)
        win.check(win.terminate_job(job, 1), 'clean maintenance descendants')
        proc.wait(timeout=5)
        reader.join(timeout=5)
        if reader.is_alive():
            raise RuntimeError('maintenance output cleanup incomplete')
        accounting = win.C.create_string_buffer(48)
        deadline = time.monotonic()+5
        while True:
            win.check(win.query_job(job, 1, accounting, 48, None), 'verify maintenance cleanup')
            if int.from_bytes(accounting.raw[40:44], 'little') == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError('maintenance processes remain')
            time.sleep(.05)
        if overflow.is_set():
            reason = 'output_limit'
        return {'state': reason, 'exit': proc.returncode,
            'seconds': round(time.monotonic()-start, 2),
            'output': b''.join(chunks).decode('utf-8', errors='replace'),
            'cleanup_confirmed': True}
    finally:
        win.terminate_job(job, 1)
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for pipe in (proc.stdin, proc.stdout):
                if pipe and not pipe.closed:
                    pipe.close()
        win.close(job)


def powershell(script, cwd, timeout=60):
    prefix = "$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$ErrorActionPreference='Stop';"
    encoded = base64.b64encode((prefix+script).encode('utf-16le')).decode()
    return bounded([str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'),
        '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text', '-EncodedCommand', encoded], cwd, timeout)


def status(cfg):
    names = cfg['tasks']
    for item in names:
        if not item.replace('-', '').isalnum():
            raise ValueError('invalid task name')
    script = "$s=New-Object -ComObject 'Schedule.Service';$s.Connect();$r=$s.GetFolder('\\');"
    script += '$names=@('+','.join("'"+n+"'" for n in names)+');'
    script += "@($names|ForEach-Object {try{$t=$r.GetTask($_);@{name=$_;state=[int]$t.State;result=$t.LastTaskResult}}catch{@{name=$_;state=0;result=-1}}})|ConvertTo-Json -Compress"
    result = powershell(script, cfg['work_dir'], 15)
    if result['state'] != 'finished' or result['exit'] != 0:
        raise RuntimeError('service status unavailable')
    tasks = json.loads(result['output'])
    conn = sqlite3.connect(Path(cfg['database']).as_uri()+'?mode=ro', uri=True, timeout=2)
    try:
        states = dict(conn.execute('select state,count(*) from tasks group by state'))
        active = conn.execute("select count(*) from executions where finished is null").fetchone()[0]
        pending = dict(conn.execute("select state,count(*) from outbox where state in ('queued','sending','unknown') group by state"))
    finally:
        conn.close()
    # Windows MEMORYSTATUSEX, no sampling daemon and no stored performance data.
    class Memory(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)]+[(n, ctypes.c_ulonglong) for n in ('total','available','commit_limit','commit_free','virtual','virtual_free','extended')]
    mem = Memory(); mem.length = ctypes.sizeof(mem)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        raise OSError('memory status unavailable')
    return {'tasks': tasks, 'active': active, 'queued': states.get('queued', 0),
        'pending_delivery': pending, 'memory_free_gb': round(mem.available/2**30, 1),
        'memory_total_gb': round(mem.total/2**30, 1),
        'commit_used_gb': round((mem.commit_limit-mem.commit_free)/2**30, 1),
        'commit_limit_gb': round(mem.commit_limit/2**30, 1)}


def control_of(cfg):
    return Path(cfg.get('control') or Path(cfg['database']).parent)


def usb_scan(cfg, run=None):
    """Removable drives, their free space, and what a backup needs; read-only."""
    from tools import backup_export as backup
    run = run or (lambda script: powershell(script, cfg['work_dir'], 20)['output'])
    control = control_of(cfg)
    return backup.scan(control, run, data_drive=os.path.splitdrive(str(control))[0])


def usb_export(cfg, drive, run=None, spawn=None, now=time.time):
    """Start the long copy as its own process (the panel's 60-second command limit does not apply) and return at once."""
    found = usb_scan(cfg, run)
    chosen = [d for d in found['drives'] if d['letter'].upper() == str(drive).upper()]
    if not chosen:
        raise ValueError('That drive is not a connected removable drive')
    if chosen[0]['free'] < found['estimate']['needed_bytes']:
        raise ValueError('Not enough free space on that drive')
    progress = Path(cfg['work_dir']) / 'usb-backup-progress.json'
    if progress.exists():
        try:
            last = json.loads(progress.read_text(encoding='utf-8'))
            if last.get('state') in ('starting', 'running') and now() - progress.stat().st_mtime < 300:
                raise RuntimeError('A backup is already running')
        except (OSError, ValueError):
            pass
    progress.write_text(json.dumps({'state': 'starting', 'message': '正在启动备份', 'done_bytes': 0,
                                    'total_bytes': found['estimate']['bytes']}, ensure_ascii=False), encoding='utf-8')
    argv = [sys.executable, '-B', '-X', 'utf8', str(Path(__file__).with_name('backup_export.py')), 'export',
            '--control', str(control_of(cfg)), '--dest', chosen[0]['letter'] + '\\', '--progress', str(progress)]
    (spawn or spawn_detached)(argv, cfg['work_dir'])
    return {'started': True, 'progress': str(progress), 'drive': chosen[0]['letter'], 'estimate': found['estimate']}


def spawn_detached(argv, cwd):
    flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)


def main():
    if sys.argv[1:] == ['--child']:
        argv = json.loads(sys.stdin.buffer.readline(32768))
        raise SystemExit(subprocess.call(argv, stdin=subprocess.DEVNULL))
    cfg = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
    action = sys.argv[2]
    if action == 'status':
        result = status(cfg)
    elif action == 'start':
        # Never restart a running task or rewrite scheduled-task configuration.
        result = status(cfg)
        stopped = [t['name'] for t in result['tasks'] if t['state'] == 3]
        for name in stopped:
            run = powershell("$s=New-Object -ComObject 'Schedule.Service';$s.Connect();$t=$s.GetFolder('\\').GetTask('"+name+"');if($t.State -eq 3){$null=$t.Run($null)}", cfg['work_dir'], 15)
            if run['exit'] != 0 or run['state'] != 'finished':
                raise RuntimeError('start failed: '+name)
        result = {'started': stopped}
    elif action == 'usb_scan':
        result = usb_scan(cfg)
    elif action == 'usb_export':
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise PermissionError('backup requires a local administrator')
        result = usb_export(cfg, json.loads(sys.stdin.buffer.readline(4096))['drive'])
    elif action == 'command':
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise PermissionError('maintenance requires a local administrator')
        command = json.loads(sys.stdin.buffer.readline(32768))['command']
        if not isinstance(command, str) or not 1 <= len(command) <= 8192:
            raise ValueError('invalid command length')
        result = powershell(command, cfg['work_dir'])
    else:
        raise ValueError('unknown panel action')
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
