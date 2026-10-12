"""Local operator panel helper used by panel_web.py: service status, starting stopped services and USB backup.
No network listener, no model-facing API and no general command runner. Existing Bot isolation is unchanged.
"""
import base64
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def powershell(script, cwd, timeout=60, max_output=65536):
    """Run one of this module's fixed status/start scripts. Not a general command runner."""
    prefix = "$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);$ErrorActionPreference='Stop';"
    encoded = base64.b64encode((prefix+script).encode('utf-16le')).decode()
    argv = [str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'),
            '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text', '-EncodedCommand', encoded]
    start = time.monotonic()
    try:
        done = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        return {'state': 'timeout', 'exit': None, 'seconds': round(time.monotonic()-start, 2), 'output': ''}
    return {'state': 'finished', 'exit': done.returncode, 'seconds': round(time.monotonic()-start, 2),
            'output': done.stdout[:max_output].decode('utf-8', errors='replace')}


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
