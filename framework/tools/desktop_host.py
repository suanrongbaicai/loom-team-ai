"""Fixed Session 0 object keeper. Never executes job commands or model text."""
import ctypes
import json
from pathlib import Path
import re
import sys
import time

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root/'Framework'))
from loom.appcontainer import private_desktop, identity, close_desktop, close_station
from loom.commands import no_links, save_journal


def main():
    path = Path(sys.argv[1]).resolve()
    if not path.is_relative_to(root/'Jobs'):
        raise ValueError('Protected job location required')
    no_links(path)
    job = json.loads(path.read_text(encoding='utf-8'))
    if not re.fullmatch(r'cmd-[0-9a-f]{32}', job['id']) or path.name != job['id']+'.json':
        raise ValueError('Command job required')
    expected = 'LOOM.Command.'+job['id']
    container = job['desktop_container']
    if container['name'] != expected or container['sid'] != identity(expected):
        raise ValueError('Container identity mismatch')
    if not re.fullmatch(r'S-1-5-21-(?:\d+-){3}\d+', sys.argv[2]):
        raise ValueError('Ordinary supervisor SID required')
    timeout = job['timeout_seconds']
    if type(timeout) is not int or not 31 <= timeout <= 150:
        raise ValueError('Bounded command timeout required')
    session = ctypes.c_ulong()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(ctypes.windll.kernel32.GetCurrentProcessId(),ctypes.byref(session)) or session.value != 0:
        raise RuntimeError('Session 0 required')
    directory = root/'Control/DesktopHosts'
    no_links(directory)
    ready = directory/(job['id']+'.json')
    stop = directory/(job['id']+'.stop')
    station = desktop = None
    result = {'id':job['id'],'state':'failed','windows_session':session.value}
    try:
        station, desktop, name = private_desktop(expected, container['sid'],sys.argv[2])
        result.update(state='ready',desktop=name)
        save_journal(ready,result)
        deadline = time.monotonic()+timeout+30
        while time.monotonic() < deadline and not stop.exists():
            time.sleep(.2)
        result['state'] = 'closed'
    except Exception as error:
        result.update(state='failed',error=str(error))
    finally:
        result['desktop_closed'] = not desktop or bool(close_desktop(desktop))
        result['station_closed'] = not station or bool(close_station(station))
        if not result['desktop_closed'] or not result['station_closed']:
            result['state'] = 'cleanup_failed'
        save_journal(ready,result)


if __name__ == '__main__':
    main()
