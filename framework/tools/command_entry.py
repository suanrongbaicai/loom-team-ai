"""Ordinary-account supervisor; container commands receive no host capability."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anc.appcontainer import run

job = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
result = run(job['name'], job['argv'], job['cwd'], job['environment'],
             timeout=job['timeout'], cancelled=lambda: Path(job['cancel']).exists(),
             max_output=job['max_output'], desktop_name=job.get('desktop_name'))
result['id'] = job['id']
target = Path(job['receipt'])
temporary = target.with_suffix('.pending')
temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
temporary.replace(target)
