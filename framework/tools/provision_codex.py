"""Download the pinned official Windows package; never modifies boot or disks."""
import argparse
import hashlib
import json
from pathlib import Path
import tarfile
import urllib.request

URL = 'https://github.com/openai/codex/releases/download/rust-v0.154.0/codex-package-x86_64-pc-windows-msvc.tar.gz'
EXPECTED = '94cc5b3632769504c809f6c0364b693c0dfddc5c30c8361095d2263a07ac45a4'


def main(ROOT, proxy=None):
    status = ROOT / 'Evidence/codex-package-status.json'
    report = {'check': 'LOOM-OFFICIAL-CODEX-PACKAGE', 'source': URL, 'state': 'starting'}
    def save():
        status.write_text(json.dumps(report), encoding='utf-8')
    save()
    try:
        archive = ROOT / 'Downloads/codex-package-0.154.0.tar.gz'
        archive.parent.mkdir(parents=True, exist_ok=True)
        target = ROOT / 'Apps/codex-0.154.0'
        if archive.exists() or target.exists():
            raise RuntimeError('Existing package must be inspected before retry')
        http = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy, 'http': proxy} if proxy else {}))
        sha = hashlib.sha256()
        size = 0
        with http.open(URL, timeout=45) as response, archive.open('xb') as output:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > 350 * 1024 * 1024:
                    raise RuntimeError('Package exceeded download bound')
                output.write(block)
                sha.update(block)
                report.update(state='downloading', bytes=size)
                save()
        report.update(sha256=sha.hexdigest(), bytes=size)
        if report['sha256'] != EXPECTED:
            raise RuntimeError('Official package SHA256 mismatch')
        target.mkdir(parents=True)
        with tarfile.open(archive) as package:
            members = package.getmembers()
            for member in members:
                if not (target / member.name).resolve().is_relative_to(target.resolve()):
                    raise RuntimeError('Package path escapes destination')
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError('Unexpected archive link or device')
            package.extractall(target, members=members, filter='data')
        report.update(state='verified-extracted', target=str(target),
                      executables=[str(p.relative_to(target)) for p in target.rglob('*.exe')])
    except Exception as error:
        report.update(state='failed', error_type=type(error).__name__, error=str(error)[:400])
    save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True, help='approved installation root (never a default)')
    parser.add_argument('--proxy', help='optional http proxy, e.g. http://127.0.0.1:<port>')
    args = parser.parse_args()
    main(args.root, args.proxy)
