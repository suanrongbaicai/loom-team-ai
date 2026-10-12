"""Build and verify the factory package (落地日志 0089/0101): the clean, versioned starting point for a new deployment.

`build` copies the framework (code, rules, skills, runtime tools, tests, licences) plus `factory/templates` into a new
directory and writes FACTORY.json (version, source commit, SHA256 of every file).  `verify` re-reads a built directory and
fails on anything that must never ship: databases, credentials and configuration with real values, runtime folders, test
residue, team identifiers, absolute production paths.  Output is file names and rule names, never matched text."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.fsguard import chain_link, is_link, sha_file as sha  # noqa: E402

SRC = Path(__file__).resolve().parents[1]
INCLUDE_DIRS = ('loom', 'rules', 'skills', 'tools', 'tests', 'licenses', 'templates')
INCLUDE_FILES = ('README.md', 'THIRD_PARTY_NOTICES.md', 'pyproject.toml', 'requirements-feishu-win-py312.lock', 'sdk-download-manifest.json')
TEMPLATES = SRC / 'factory' / 'templates'
EXCLUDE_NAMES = {'__pycache__', '.test-work', 'purge_test_data.py', 'test_purge_test_data.py'}
FORBIDDEN_SUFFIXES = ('.sqlite', '.sqlite-wal', '.sqlite-shm', '.db', '.pyc', '.pem', '.key', '.pfx')
FORBIDDEN_NAMES = {'controller.json', 'gateway.json', 'ingress.key', 'worker-credentials.json', 'auth.json', '.env',
                   '.controller.lock', '.gateway.lock', 'config.toml'}
FORBIDDEN_DIRS = {'.test-work', '__pycache__', 'evidence', 'qa', 'wechatruntime', 'codexhome', 'workers', 'jobs'}   # compared in lower case
# (rule name, pattern).  The second pattern set only applies outside tests, where fake ids are legitimate.
CONTENT_RULES = (
    ('feishu-app-id', re.compile(r'\bcli_[0-9a-f]{16}\b')),
    ('feishu-open-id', re.compile(r'\bou_[0-9a-f]{24,}\b')),
    ('wechat-id', re.compile(r'\bwxid_[a-z0-9]{8,}\b')),
    ('secret-assignment', re.compile(r'(?i)["\']?(app_secret|password|api[_-]?key|access[_-]?token|private[_-]?key)["\']?\s*[:=]\s*["\'][A-Za-z0-9_\-+/=]{16,}["\']')),
    ('production-root', re.compile(r'''(?i)[A-Za-z]:[\\/]+[^\\/\s"']+[\\/]+(Control|Workers|Evidence|Jobs|QA|Framework)''')),
)
MAX_SCAN_BYTES = 16 * 1024 * 1024
TEXT_SUFFIXES = {'.py', '.md', '.json', '.toml', '.txt', '.ps1', '.cs', '.lock', '.yml', '.yaml', '.template', ''}
ALLOWED_CONTENT = {   # (relative path, rule) pairs that are deliberate: the scan itself names its own patterns
    ('tools/factory.py', 'feishu-app-id'), ('tools/factory.py', 'feishu-open-id'), ('tools/factory.py', 'wechat-id'),
    ('tools/factory.py', 'secret-assignment'), ('tools/factory.py', 'production-root'),
    ('tests/test_factory.py', 'feishu-app-id'), ('tests/test_factory.py', 'wechat-id'), ('tests/test_factory.py', 'secret-assignment'),
    ('tests/test_factory.py', 'production-root'), ('tests/test_public_web.py', 'wechat-id'),
}


def decode(path):
    """Text of a file in any common encoding, or None when it is binary."""
    data = Path(path).read_bytes()
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode('utf-16', errors='replace')
    if len(data) >= 4 and data[1::2].count(0) > len(data) // 4 and data[0::2].count(0) == 0:
        return data.decode('utf-16-le', errors='replace')           # BOM-less UTF-16 with mostly ASCII text
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return None


def collect(root=SRC):
    link = chain_link(root)
    if link:
        raise SystemExit('Link on the source path: ' + link)
    files = []
    for name in INCLUDE_DIRS + INCLUDE_FILES:
        entry = Path(root) / name
        if (entry.exists() or entry.is_symlink()) and is_link(entry):
            raise SystemExit('Link at a package entry point: ' + name)
    for name in INCLUDE_DIRS:
        base = Path(root) / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob('*')):
            if is_link(path):
                raise SystemExit('Link inside the source tree: ' + path.relative_to(root).as_posix())
            if path.is_file() and not (set(path.relative_to(root).parts) & EXCLUDE_NAMES) and not path.name.endswith('.pyc'):
                files.append(path.relative_to(root))
    for name in INCLUDE_FILES:
        if (Path(root) / name).is_file():
            files.append(Path(name))
    return files


def build(out, version, commit, root=SRC, templates=TEMPLATES):
    out = Path(out)
    if chain_link(out):
        raise SystemExit('Link on the output path: ' + chain_link(out))
    if out.exists() and any(out.iterdir()):
        raise SystemExit('Output directory must be new or empty')
    copied = []
    for rel in collect(root):
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(root) / rel, target)
        copied.append(rel.as_posix())
    if Path(templates).is_dir() and chain_link(templates):
        raise SystemExit('Link on the templates path: ' + chain_link(templates))
    for path in sorted(Path(templates).rglob('*')) if Path(templates).is_dir() else []:
        if is_link(path):
            raise SystemExit('Link inside the templates: ' + path.name)
        if path.is_file():
            rel = Path('deploy') / path.relative_to(templates)
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out / rel)
            copied.append(rel.as_posix())
    manifest = {'factory_version': version, 'source_commit': commit, 'built': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                'files': {rel: sha(out / rel) for rel in sorted(copied)}}
    (out / 'FACTORY.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    return manifest


def verify(directory):
    directory = Path(directory)
    problems = []
    if chain_link(directory):
        return {'ok': False, 'problems': ['link on the package path: ' + chain_link(directory)]}
    manifest_path = directory / 'FACTORY.json'
    if not manifest_path.is_file():
        return {'ok': False, 'problems': ['FACTORY.json missing']}
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    on_disk = set()
    for path in sorted(directory.rglob('*')):
        rel = path.relative_to(directory).as_posix()
        if is_link(path):
            problems.append('link: ' + rel)
            continue
        if path.is_dir():
            if path.name.lower() in FORBIDDEN_DIRS:
                problems.append('forbidden directory: ' + rel)
            continue
        if rel == 'FACTORY.json':
            continue
        on_disk.add(rel)
        if path.name.lower() in FORBIDDEN_NAMES or path.name.lower().endswith(FORBIDDEN_SUFFIXES):
            problems.append('forbidden file: ' + rel)
            continue
        if manifest['files'].get(rel) != sha(path):
            problems.append('not in manifest or changed: ' + rel)
        size = path.stat().st_size
        if size > MAX_SCAN_BYTES:
            problems.append('too large to scan: ' + rel)
            continue
        text = decode(path)
        if text is None:
            if path.suffix.lower() in TEXT_SUFFIXES:
                problems.append('text file that cannot be decoded: ' + rel)
            continue
        for rule, pattern in CONTENT_RULES:
            if pattern.search(text) and (rel, rule) not in ALLOWED_CONTENT:
                problems.append('content rule %s: %s' % (rule, rel))
    for rel in set(manifest['files']) - on_disk:
        problems.append('listed but missing: ' + rel)
    skills = directory / 'skills'
    for skill in sorted(p for p in skills.iterdir() if p.is_dir()) if skills.is_dir() else []:
        if not (skill / 'SKILL.md').is_file():
            problems.append('skill without SKILL.md: ' + skill.name)
    return {'ok': not problems, 'problems': problems, 'files': len(on_disk), 'factory_version': manifest.get('factory_version')}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('build', 'verify'))
    parser.add_argument('--out')
    parser.add_argument('--dir')
    parser.add_argument('--version')
    parser.add_argument('--commit')
    args = parser.parse_args(argv)
    if args.command == 'build':
        manifest = build(args.out, args.version, args.commit)
        report = verify(args.out)
        print(json.dumps({'files': len(manifest['files']), 'verify': report}, ensure_ascii=False, indent=1))
        return 0 if report['ok'] else 1
    report = verify(args.dir)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
