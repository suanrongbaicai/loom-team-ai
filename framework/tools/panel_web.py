"""The work panel as a small local web page (落地日志 0122).

It runs on the same Windows computer as the assistant and listens only on 127.0.0.1: no internet is
needed, nothing outside this computer can reach it, and every action (status, model changes, USB backup, restart) is local.
It opens in an Edge "app window" so it looks like an ordinary window.  Showing status is open to anyone at the computer;
changes are audited and refuse to run when they would cut off work in progress.

    python panel_web.py serve --config <panel.json> [--port 9771]
    python panel_web.py open  --config <panel.json> [--port 9771]     starts the server if needed and opens the window"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anc import member_settings, panel_data  # noqa: E402
from anc.control import Control  # noqa: E402

PAGE = Path(__file__).with_name('panel_web.html')
DEFAULT_PORT = 9771


class Services:
    """Windows side: scheduled-task status, restart, USB backup.  Replaced by a fake in tests."""
    def __init__(self, cfg):
        self.cfg = cfg

    def status(self):
        from tools import panel_bridge
        return panel_bridge.status(self.cfg)

    def start_stopped(self):
        from tools import panel_bridge
        started = []
        for task in self.status()['tasks']:
            if task['state'] == 3:
                run = panel_bridge.powershell("$s=New-Object -ComObject 'Schedule.Service';$s.Connect();$t=$s.GetFolder('\\')"
                                              ".GetTask('%s');if($t.State -eq 3){$null=$t.Run($null)}" % task['name'], self.cfg['work_dir'], 15)
                if run['exit'] != 0:
                    raise RuntimeError('启动失败：' + task['name'])
                started.append(task['name'])
        return started

    def restart_bot(self):
        from tools import panel_bridge
        names = [n for n in self.cfg['tasks'] if n.startswith('LOOM-Controller') or n.startswith('LOOM-Gateway')]
        for name in names:
            panel_bridge.powershell("$s=New-Object -ComObject 'Schedule.Service';$s.Connect();$t=$s.GetFolder('\\').GetTask('%s');"
                                    "$null=$t.Stop(0)" % name, self.cfg['work_dir'], 30)
        time.sleep(3)
        for name in names:      # controller first (listed first), then the gateways
            panel_bridge.powershell("$s=New-Object -ComObject 'Schedule.Service';$s.Connect();$t=$s.GetFolder('\\').GetTask('%s');"
                                    "$null=$t.Run($null)" % name, self.cfg['work_dir'], 30)
        return names

    def usb_scan(self):
        from tools import panel_bridge
        return panel_bridge.usb_scan(self.cfg)

    def usb_export(self, drive):
        from tools import panel_bridge
        return panel_bridge.usb_export(self.cfg, drive)


class Panel:
    def __init__(self, cfg, services=None, control=None):
        self.cfg = cfg
        self.services = services or Services(cfg)
        self._control = control
        self.lock = threading.Lock()

    def control(self):
        with self.lock:
            if self._control is None:
                root = Path(self.cfg.get('control') or Path(self.cfg['database']).parent)
                self._control = Control(root)
            return self._control

    def controller_config(self):
        path = self.cfg.get('controller_config') or (Path(self.cfg.get('control') or Path(self.cfg['database']).parent) / 'controller.json')
        try:
            return json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return {}

    def state(self):
        c = self.control()
        config = self.controller_config()
        out = {'models': list(panel_data.allowed_models(config)), 'levels': list(member_settings.LEVELS)}
        try:
            out['services'] = self.services.status()
        except Exception as error:
            out['services'] = None
            out['services_error'] = type(error).__name__
        with c.db.transaction() as db:
            policy = c.db.get_policy(db)
            out['overview'] = panel_data.overview(db)
            out['members'] = panel_data.members(db, policy, config)
        health = Path(self.cfg.get('control') or Path(self.cfg['database']).parent) / 'health.json'
        try:
            out['health'] = json.loads(health.read_text(encoding='utf-8'))['health']
        except (OSError, ValueError, KeyError):
            out['health'] = None
        return out

    def act(self, name, body):
        c = self.control()
        config = self.controller_config()
        if name == 'set_model':
            return member_settings.set_model(c, body['member'], body.get('model') or None, body.get('reasoning') or None,
                                             allowed=panel_data.allowed_models(config))
        if name == 'reset_model':
            member_settings.reset(c, body['member'])
            return {'ok': True}
        if name == 'member':
            return panel_data.change_member(c, body['member'], body['action'], body.get('role'))
        if name == 'recover':
            return {'started': self.services.start_stopped()}
        if name == 'restart':
            with c.db.transaction() as db:
                if panel_data._busy(db):
                    raise panel_data.Busy('机器人正在处理任务，现在重启会打断它们。请稍后再试。')
            return {'restarted': self.services.restart_bot()}
        if name == 'usb_scan':
            return self.services.usb_scan()
        if name == 'usb_export':
            return self.services.usb_export(body['drive'])
        raise ValueError('未知操作')


class DemoServices:
    """Pretend services for trying the window on any computer without touching a real installation."""
    def status(self):
        return {'tasks': [{'name': 'LOOM-Controller-OwnerWork', 'state': 4, 'result': 0}, {'name': 'LOOM-Gateway-Member', 'state': 4, 'result': 0},
                          {'name': 'LOOM-Gateway-NewOwner', 'state': 4, 'result': 0}, {'name': 'LOOM-ModelProxy', 'state': 4, 'result': 0}],
                'memory_free_gb': 6.2, 'memory_total_gb': 16.0, 'active': 0, 'queued': 0}

    def start_stopped(self):
        return []

    def restart_bot(self):
        return ['demo']

    def usb_scan(self):
        return {'drives': [], 'estimate': {'needed_bytes': 0}}

    def usb_export(self, drive):
        raise ValueError('演示模式不会备份')


def demo_panel():
    """A throw-away installation with invented people, for looking at the window and trying the buttons."""
    import tempfile
    root = Path(tempfile.mkdtemp(prefix='anc-panel-demo-'))
    control = Control(root / 'control')
    person = lambda extra=None: dict({'active': True, 'verified': True, 'roles': [], 'bindings': []}, **(extra or {}))
    state = {'epoch': 0, 'roster_version': 0, 'governance_confirmed': False, 'grants': [], 'apps': {},
             'members': {'owner': person({'team_member': True}), 'member': person({'team_member': True, 'roles': ['运营']}),
                         'u-demo01': person({'team_member': True, 'auto_basic': True})},
             'scopes': {'owner-private': {'kind': 'private', 'participants': ['owner'], 'membership_verified': True, 'root': str(root / 'r1')},
                        'member-private': {'kind': 'private', 'participants': ['member'], 'membership_verified': True, 'root': str(root / 'r2')}}}
    control.set_policy(state, 'demonstration data')
    (root / 'controller.json').write_text(json.dumps({'allowed_models': ['示例模型 A', '示例模型 B'],
                                                      'scopes': {'owner-private': {'model': '示例模型 B', 'reasoning': 'high'},
                                                                 'member-private': {'model': '示例模型 A', 'reasoning': 'medium'}},
                                                      'shared_worker_scope': 'owner-private',
                                                      'shared_worker_overrides': {'model': '示例模型 A', 'reasoning': 'medium'}}), encoding='utf-8')
    cfg = {'database': str(root / 'control' / 'runtime.sqlite'), 'control': str(root / 'control'), 'controller_config': str(root / 'controller.json'),
           'work_dir': str(root), 'tasks': []}
    return Panel(cfg, DemoServices(), control)


def make_handler(panel, port):
    allowed_hosts = {'127.0.0.1:%d' % port, 'localhost:%d' % port}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'ANCPanel'

        def log_message(self, *args):
            pass

        def send(self, code, payload, kind='application/json; charset=utf-8'):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            return self.headers.get('Host', '') in allowed_hosts      # blocks DNS-rebinding from a web page

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, {'error': 'host'})
            if self.path in ('/', '/index.html'):
                return self.send(200, PAGE.read_bytes(), 'text/html; charset=utf-8')
            if self.path == '/api/state':
                try:
                    return self.send(200, panel.state())
                except Exception as error:
                    return self.send(500, {'error': type(error).__name__ + ': ' + str(error)[:200]})
            self.send(404, {'error': 'not found'})

        def do_POST(self):
            if not self.host_ok() or self.headers.get('X-ANC-Panel') != '1':     # a web page cannot set this header cross-site
                return self.send(403, {'error': 'forbidden'})
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 <= length <= 16384:
                return self.send(400, {'error': 'size'})
            try:
                body = json.loads(self.rfile.read(length) or b'{}')
                result = panel.act(self.path.rsplit('/', 1)[-1], body)
                self.send(200, {'ok': True, 'result': result})
            except (panel_data.Busy, ValueError, KeyError, PermissionError) as error:
                self.send(409, {'ok': False, 'error': str(error) or type(error).__name__})
            except Exception as error:
                self.send(500, {'ok': False, 'error': type(error).__name__ + ': ' + str(error)[:200]})
    return Handler


def serve(cfg, port=DEFAULT_PORT, panel=None):
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(panel or Panel(cfg), port))
    server.daemon_threads = True
    return server


def port_open(port):
    with socket.socket() as probe:
        probe.settimeout(.3)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def open_window(cfg, port=DEFAULT_PORT, spawn=True):
    if spawn and not port_open(port):
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve', '--config', cfg['_path'], '--port', str(port)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        for _ in range(40):
            if port_open(port):
                break
            time.sleep(.25)
    url = 'http://127.0.0.1:%d/' % port
    edge = Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
    if edge.is_file():
        subprocess.Popen([str(edge), '--app=' + url, '--window-size=440,780'], close_fds=True)
    else:
        import webbrowser
        webbrowser.open(url)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('serve', 'open'))
    parser.add_argument('--config')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--demo', action='store_true', help='invented data and fake services, touches nothing real')
    args = parser.parse_args(argv)
    if args.demo:
        cfg = {'_path': ''}
        if args.command == 'serve':
            serve(cfg, args.port, demo_panel()).serve_forever()
        else:
            threading.Thread(target=serve(cfg, args.port, demo_panel()).serve_forever, daemon=True).start()
            open_window(dict(cfg, _demo=True), args.port, spawn=False)
            time.sleep(3600)
        return 0
    if not args.config:
        parser.error('--config is required')
    cfg = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    cfg['_path'] = str(Path(args.config).resolve())
    if args.command == 'serve':
        serve(cfg, args.port).serve_forever()
    else:
        open_window(cfg, args.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
