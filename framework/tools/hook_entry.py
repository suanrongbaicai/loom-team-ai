"""Absolute managed-hook entry; import only the administrator-installed runtime."""
from pathlib import Path
import ctypes
import os
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.hook import main

if os.name == 'nt':
    name = ctypes.create_unicode_buffer(256)
    size = ctypes.c_ulong(256)
    if not ctypes.windll.advapi32.GetUserNameW(name, ctypes.byref(size)):
        raise RuntimeError('Cannot verify local worker identity')
    managed_worker = name.value.lower().startswith('loom_')
else:
    managed_worker = bool(os.environ.get('LOOM_TASK_TOKEN'))
if os.environ.get('LOOM_HOOK_DIAGNOSTICS'):
    # Metadata only; never log tokens, raw hook payloads or business text.
    with (Path(os.environ['TEMP']) / 'loom-hook-dispatch.jsonl').open('a', encoding='utf-8') as diagnostic:
        diagnostic.write(json.dumps({'managed_worker': managed_worker,
                         'token_present': bool(os.environ.get('LOOM_TASK_TOKEN')),
                         'endpoint_present': bool(os.environ.get('LOOM_ENDPOINT'))}) + '\n')
if managed_worker:
    main()
else:
    # This host-wide managed hook leaves unrelated official CLI users alone.
    print(json.dumps({}))
