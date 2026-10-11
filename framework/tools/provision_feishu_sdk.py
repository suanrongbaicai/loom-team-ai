"""Install pinned SDK wheels into the approved installation root only (given with --root)."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.parse
import urllib.request


def main(root, proxy=None):
    root = Path(root)
    framework = root / 'Framework'
    cache = root / 'Cache/feishu-sdk'
    target = root / 'Apps/feishu-sdk-1.7.3'
    scratch = root / 'Cache/sdk-install-temp'
    for p in (cache, target, scratch):
        if not p.resolve().is_relative_to(root.resolve()) or p.is_symlink() or p.is_junction():
            raise RuntimeError('Unexpected installation target')
    if target.exists():
        raise RuntimeError('SDK destination already exists; inspect before reuse')
    cache.mkdir(parents=True, exist_ok=True)
    scratch.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((framework / 'sdk-download-manifest.json').read_text())
    http = urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy} if proxy else {}))
    installer = None
    for item in manifest:
        url = urllib.parse.urlsplit(item['url'])
        if url.scheme != 'https' or url.hostname != 'files.pythonhosted.org' or Path(item['file']).name != item['file']:
            raise RuntimeError('Unexpected wheel source')
        p = cache / item['file']
        if not p.exists():
            with http.open(item['url'], timeout=45) as response:
                data = response.read(item['bytes'] + 1)
            if len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                raise RuntimeError('Downloaded wheel does not match pinned artifact')
            p.write_bytes(data)
        elif p.stat().st_size != item['bytes'] or hashlib.sha256(p.read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError('Cached wheel differs from pinned artifact')
        if item.get('installer_only'):
            installer = p
        print(json.dumps({'verified': item['file']}), flush=True)
    if installer is None:
        raise RuntimeError('Pinned installer missing')
    env = dict(os.environ, TMP=str(scratch), TEMP=str(scratch), PIP_CONFIG_FILE=os.devnull,
               PIP_DISABLE_PIP_VERSION_CHECK='1', PYTHONDONTWRITEBYTECODE='1')
    code = 'import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_module("pip",run_name="__main__")'
    argv = [sys.executable, '-B', '-s', '-c', code, str(installer), '--isolated', 'install',
            '--no-index', '--no-cache-dir', '--no-compile', '--only-binary=:all:', '--require-hashes',
            '--find-links', str(cache), '--target', str(target),
            '--report', str(root/'Evidence/feishu-sdk-install-report.json'),
            '-r', str(framework/'requirements-feishu-win-py312.lock')]
    subprocess.run(argv, env=env, check=True, timeout=300)
    sys.path.insert(0, str(target))
    import lark_oapi, websockets, requests, httpx
    from Crypto.Cipher import AES
    AES.new(b'0'*16, AES.MODE_ECB).encrypt(b'0'*16)
    result = {'check': 'LOOM-FEISHU-SDK-INSTALL', 'state': 'installed', 'target': str(target),
              'versions': {n: importlib.metadata.version(n) for n in ('lark-oapi', 'websockets', 'requests', 'httpx')},
              'bytes': sum(p.stat().st_size for p in target.rglob('*') if p.is_file()), 'native_crypto': True}
    (root/'Evidence/feishu-sdk-install-status.json').write_text(json.dumps(result), encoding='utf-8')
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True, help='approved installation root (never a default)')
    parser.add_argument('--proxy', help='optional http proxy, e.g. http://127.0.0.1:<port>')
    args = parser.parse_args()
    main(args.root, args.proxy)
