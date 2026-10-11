"""Identify live WeChat accounts from handles, without statting unrelated files."""
from pathlib import PureWindowsPath
import re

from .policy import Denied


def roots_from_paths(paths):
    roots = set()
    for path in paths:
        if not isinstance(path, str):
            raise Denied('Live account handle path unavailable')
        value = str(PureWindowsPath(path))
        match = re.match(r'^(.*?\\xwechat_files\\[^\\]+)\\(?:db_storage|msg)(?:\\|$)', value, re.I)
        if match:
            root = PureWindowsPath(match.group(1))
            if not root.is_absolute() or '..' in root.parts:
                raise Denied('Live account handle path is not absolute')
            roots.add(str(root).casefold())
    return roots


def recheck_live(identity):
    import psutil
    from psutil import _pswindows as win
    # This private API is intentionally tied to the inspected target version.
    if psutil.__version__ != '7.2.2':
        raise Denied('Live account reader requires reviewed psutil version')
    roots = set()
    try:
        for proc in psutil.process_iter(['pid', 'name']):
            if (proc.info.get('name') or '').casefold() != 'weixin.exe':
                continue
            try:
                born = proc.create_time()
                # psutil.Process.open_files() additionally stats *every* handle.
                # An unrelated inaccessible file must not hide account handles.
                paths = [win.convert_dos_path(p) for p in win.cext.proc_open_files(proc.pid)]
                if not proc.is_running() or proc.create_time() != born:
                    raise Denied('WeChat process changed during identity check')
                roots.update(roots_from_paths(paths))
            except psutil.NoSuchProcess:
                raise Denied('WeChat process disappeared during identity check') from None
    except Denied:
        raise
    except Exception:
        raise Denied('Live account handle query unavailable') from None
    expected = str(PureWindowsPath(identity['account_root'])).casefold()
    if roots != {expected}:
        raise Denied('Live account changed or is not unique')
