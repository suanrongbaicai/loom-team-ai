"""Scope-user child. Job/environment paths are protected by the controller ACL."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def write_result(path, result):
    """Replace result.json whole so the controller never reads a half-written file.

    The temp name differs from result.json, so a reader only ever sees the old or
    the complete new content. On Windows a reader may briefly block the replace;
    retry, then fall back to a direct write (reader treats unreadable as 'alive').
    """
    data = json.dumps(result).encode('utf-8')
    temp = path.with_name(path.name + '.tmp')
    for _ in range(40):
        try:
            temp.write_bytes(data)
            os.replace(temp, path)
            return
        except OSError:
            time.sleep(0.05)
    path.write_bytes(data)


def mark_first_stdout(output_path, marks):
    """When the CLI's event file first holds bytes.  This is the first stdout the controller *observed* (polled about every
    half second; the first bytes may be a start event, not model text), so it is not a time-to-first-token figure."""
    if marks is not None and output_path is not None and 'first_stdout_seen_at' not in marks:
        try:
            if os.stat(output_path).st_size > 0:
                marks['first_stdout_seen_at'] = time.time()
        except OSError:
            pass


def wait_child(process, prompt, timeout, cancel_path, output_path=None, marks=None):
    deadline = time.monotonic() + timeout
    first = True
    while True:
        try:
            process.communicate(prompt if first else None, timeout=0.5)
            mark_first_stdout(output_path, marks)     # a child that prints and exits inside one poll still gets a mark
            return 'exited'
        except subprocess.TimeoutExpired:
            first = False
            mark_first_stdout(output_path, marks)
        cancelled = cancel_path.exists()
        if cancelled or time.monotonic() >= deadline:
            # Keep Popen's process handle alive while terminating this exact
            # child and its descendants. Do not rediscover processes by name.
            if os.name == 'nt':
                stopped = subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                                         capture_output=True, timeout=15)
                if stopped.returncode and process.poll() is None:
                    raise RuntimeError('Child process tree stop unconfirmed')
            else:
                process.kill()
            process.wait(timeout=15)
            return 'cancelled' if cancelled else 'timed_out'


def main():
    job_path = Path(sys.argv[1])
    job = json.loads(job_path.read_text(encoding='utf-8-sig'))
    cancel_path = job_path.with_suffix('.cancel')
    # Keep SDK, CLI sessions, package caches and temporary outputs on the approved disk.
    os.environ.update(job['environment'])
    for name in ('TEMP', 'TMP', 'CODEX_HOME', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE'):
        Path(os.environ[name]).mkdir(parents=True, exist_ok=True)
    cwd = Path(job['workspace'])
    cwd.mkdir(parents=True, exist_ok=True)
    result = {'worker_pid': os.getpid(), 'job': job['id'], 'state': 'starting', 'worker_started_at': time.time()}   # timing marks: where do the start-up seconds go
    if os.name == 'nt':
        import ctypes
        session = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(),ctypes.byref(session)):
            result['windows_session'] = session.value
    output_dir = Path(job['output_dir'])
    output_dir.mkdir(parents=True, exist_ok=True)
    result_path = output_dir / 'result.json'
    write_result(result_path, result)
    with (output_dir / 'events.jsonl').open('wb') as output, (output_dir / 'stderr.txt').open('wb') as error:
        try:
            if cancel_path.exists():
                result.update(state='cancelled')
                write_result(result_path, result)
                return
            if job.get('require_anc_preflight'):
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                from anc.client import call
                result['preflight_started_at'] = time.time()
                if not call('preflight', {}).get('complete'):
                    raise RuntimeError('Full rules preflight incomplete')
                result['preflight_returned_at'] = time.time()
            popen_began = time.time()
            process = subprocess.Popen(job['argv'], cwd=cwd, env=os.environ,
                                       stdin=subprocess.PIPE, stdout=output, stderr=error,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            result.update(state='running', cli_pid=process.pid, cli_started_at=time.time(), popen_began_at=popen_began)
            write_result(result_path, result)
            marks = {}
            state = wait_child(process, job['prompt'].encode('utf-8'),
                               job.get('timeout_seconds', 1200), cancel_path, output_dir / 'events.jsonl', marks)
            result.update(state=state, exit_code=process.returncode, cli_ended_at=time.time(), **marks)
        except subprocess.TimeoutExpired:
            if os.name == 'nt':
                subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
            else:
                process.kill()
            result.update(state='timed_out')
        except Exception as exc:
            result.update(state='failed', error_type=type(exc).__name__,
                          error=((str(exc).strip().splitlines() or [''])[0])[:200])
    write_result(result_path, result)


if __name__ == '__main__':
    main()
