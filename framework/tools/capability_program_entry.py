"""Executed INSIDE the existing offline command container, never by an admin."""
import json
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import threading


def execute_program(path, entrypoint, value, env):
    # The enclosing Windows Job owns every descendant. Bound pipe collection
    # here too, so a child cannot allocate arbitrary output in this helper.
    with tempfile.TemporaryFile(dir=path.parent) as input_file:
        input_file.write(json.dumps(value,ensure_ascii=False).encode()); input_file.seek(0)
        proc=subprocess.Popen([sys.executable,'-B','-X','utf8',str(path.parent/entrypoint)],
                              stdin=input_file,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                              cwd=path.parent,env=env)
        buffers=[bytearray(),bytearray()]; lock=threading.Lock(); exceeded=threading.Event()
        def collect(stream,index):
            try:
                while True:
                    chunk=stream.read(8192)
                    if not chunk:break
                    with lock:
                        if sum(map(len,buffers))+len(chunk)>256*1024:
                            exceeded.set();proc.kill();break
                        buffers[index].extend(chunk)
            finally:stream.close()
        readers=[threading.Thread(target=collect,args=(s,i),daemon=True) for i,s in enumerate((proc.stdout,proc.stderr))]
        for thread in readers:thread.start()
        try:proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait();raise ValueError('程序超时；本次执行停止。')
        for thread in readers:thread.join(timeout=1)
        if exceeded.is_set() or any(t.is_alive() for t in readers):
            raise ValueError('程序输出超限或子进程未退出；本次执行停止。')
        return proc.returncode,*(b.decode('utf-8',errors='replace') for b in buffers)


def run(spec_path):
    path=Path(spec_path).resolve(); spec=json.loads(path.read_text(encoding='utf-8'))
    env=dict(os.environ, PYTHONPATH=str(path.parent/'deps'), PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    cases=spec.get('cases')
    inputs=[c['input'] for c in cases] if cases is not None else [spec['input']]
    results=[]
    for value in inputs:
        try:code,stdout,stderr=execute_program(path,spec['entrypoint'],value,env)
        except ValueError as e:
            print(json.dumps({'ok':False,'stage':'program','detail':str(e)},ensure_ascii=False));return 1
        if code:
            print(json.dumps({'ok':False,'stage':'program','stderr':stderr[-4000:]},ensure_ascii=False));return 1
        try: results.append(json.loads(stdout))
        except ValueError:
            print(json.dumps({'ok':False,'stage':'output','detail':'程序应输出一份JSON。','stdout':stdout[-2000:]},ensure_ascii=False));return 1
        if len(json.dumps(results,ensure_ascii=False).encode())>512*1024:
            print(json.dumps({'ok':False,'stage':'output','detail':'检查总输出超限。'},ensure_ascii=False));return 1
    if cases is not None:
        matches=[r==c['expected'] for r,c in zip(results,cases)]
        print(json.dumps({'ok':all(matches),'case_results':matches,'actual':results},ensure_ascii=False))
        return 0 if all(matches) else 1
    print(json.dumps({'ok':True,'result':results[0]},ensure_ascii=False));return 0


if __name__=='__main__':
    raise SystemExit(run(sys.argv[1]))
