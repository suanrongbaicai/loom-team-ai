"""Shared filesystem helpers for administrator tools."""
import hashlib
import os
from pathlib import Path


def is_link(path):
    path = Path(path)
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        return True
    try:
        return bool(getattr(os.lstat(path), 'st_file_attributes', 0) & 0x400)     # Windows reparse point
    except OSError:
        return False


def chain_link(path):
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.relative_to(path.anchor).parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and is_link(current):
            return str(current)
    return None


def sha_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()
