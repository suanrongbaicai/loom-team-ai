"""Run offline framework checks; external socket connections are refused."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
FRAMEWORK = ROOT / 'framework'
sys.path.insert(0, str(FRAMEWORK))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quick', action='store_true', help='Check core permission, records, sending, model routing and the new demo')
    parser.add_argument('--json', type=Path)
    args = parser.parse_args()
    connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple):
            try:
                allowed = ipaddress.ip_address(address[0]).is_loopback
            except ValueError:
                allowed = address[0] == 'localhost'
            if not allowed:
                raise AssertionError('Offline tests cannot connect to external hosts')
        return connect(sock, address)

    socket.socket.connect = local_only
    os.chdir(FRAMEWORK)
    try:
        loader = unittest.TestLoader()
        if args.quick:
            suite = loader.loadTestsFromNames([
                'tests.test_control', 'tests.test_recording', 'tests.test_model_backend',
                'tests.test_team_open', 'tests.test_link_guard', 'tests.test_secret_guard'])
        else:
            suite = loader.discover('tests', top_level_dir='.')
        sys.path.insert(0, str(ROOT / 'examples'))
        suite.addTests(loader.discover(str(ROOT / 'examples'), pattern='test_*.py', top_level_dir=str(ROOT / 'examples')))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    finally:
        socket.socket.connect = connect
    summary = {
        'mode': 'quick' if args.quick else 'full', 'python': sys.version.split()[0],
        'platform': sys.platform, 'tests': result.testsRun,
        'passed': result.testsRun - len(result.failures) - len(result.errors) - len(result.skipped) - len(result.expectedFailures) - len(result.unexpectedSuccesses),
        'failures': [{'test': str(t), 'detail': s} for t, s in result.failures],
        'errors': [{'test': str(t), 'detail': s} for t, s in result.errors],
        'skipped': [{'test': str(t), 'reason': s} for t, s in result.skipped],
        'expected_failures': [str(t) for t, _ in result.expectedFailures],
        'unexpected_successes': [str(t) for t in result.unexpectedSuccesses],
        'external_network_blocked_in_runner': True,
    }
    if args.json:
        out = args.json.absolute()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in summary.items() if k not in ('failures', 'errors', 'skipped')}, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
