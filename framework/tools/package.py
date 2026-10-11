"""Build only new reusable runtime sources, never credentials or team records."""
import hashlib
import json
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
out = root.parent / 'artifacts'
out.mkdir(exist_ok=True)
target = out / 'loom-framework-dev1.zip'
files = []
with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for top in ('loom', 'rules', 'skills', 'tests', 'tools'):
        for path in sorted((root / top).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                rel = path.relative_to(root).as_posix()
                archive.write(path, rel)
                files.append(rel)
    for name in ('pyproject.toml', 'THIRD_PARTY_NOTICES.md'):
        archive.write(root / name, name)
        files.append(name)
receipt = {'file': target.name, 'bytes': target.stat().st_size,
           'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'files': files}
target.with_suffix('.manifest.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({k: v for k, v in receipt.items() if k != 'files'}))
