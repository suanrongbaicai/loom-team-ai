from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from loom.control import Control
from tools import trial_report

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def at(seconds):
    return (NOW - timedelta(hours=1) + timedelta(seconds=seconds)).isoformat()


class TrialReportTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.c = Control(Path(self.temp.name) / 'control')
        self.path = self.c.root / 'runtime.sqlite'
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            # turn: wait, model, first reply, final reply, tool calls
            for n, (wait, model, first, final, tools) in enumerate([(2, 30, 10, 40, 4), (3, 50, 20, 60, 12), (1, 20, None, None, 1)]):
                task = 't%d' % n
                db.execute("INSERT INTO tasks(id,scope,actor,kind,state,epoch,created) VALUES(?,?,?,?,?,?,?)",
                           (task, 's', 'alice', 'turn', 'done', 0, at(0)))
                db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,NULL)', (task, 'j', 'o', 'finished', at(wait), at(wait + model)))
                db.execute('INSERT INTO usage_log VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (task, 's', 'alice', 'turn', 'm', 'high', 10000, 8000, 300, 100, tools, at(wait), at(wait + model), at(100)))
                for offset in (first, final):
                    if offset is not None:
                        db.execute("INSERT INTO audit(time,actor,task,action,detail) VALUES(?,?,?,?,?)", (at(offset), 'transport', task, 'reply.sent', '{}'))
                for _ in range(tools):
                    db.execute("INSERT INTO audit(time,actor,task,action,detail) VALUES(?,?,?,?,?)", (at(5), 'alice', task, 'document.read', '{}'))
            db.execute("INSERT INTO tasks(id,scope,actor,kind,state,epoch,created) VALUES('old','s','alice','turn','done',0,?)",
                       ((NOW - timedelta(days=30)).isoformat(),))
            db.execute("INSERT INTO member_preferences VALUES('alice','先说结论',?)", (at(0),))
            db.execute("INSERT INTO share_requests VALUES('share-1','alice','p','{}','approved',?,NULL)", (at(0),))

    def tearDown(self):
        self.temp.cleanup()

    def test_latency_is_split_into_phases_within_the_window(self):
        result = trial_report.report(self.path, 7, NOW)
        self.assertEqual(result['window']['turns'], 3)
        self.assertEqual(result['window']['answered'], 2)
        self.assertEqual(result['latency_seconds']['model'], {'count': 3, 'p50': 30.0, 'p90': 50.0, 'min': 20.0, 'max': 50.0})
        self.assertEqual(result['latency_seconds']['final_reply']['max'], 60.0)
        self.assertEqual(result['latency_seconds']['first_reply']['min'], 10.0)
        self.assertEqual([b['tool_calls'] for b in result['by_tool_calls']], ['0-2', '3-5', '11-20'])
        self.assertEqual(result['tokens']['cached_share'], 0.8)
        self.assertAlmostEqual(result['actions_per_turn']['document.read'], 17 / 3, places=2)
        self.assertNotIn('reply.sent', result['actions_per_turn'])
        self.assertEqual(result['features']['member_preferences'], 1)
        self.assertEqual(result['features']['share_requests'], {'approved': 1})

    def test_the_database_is_never_modified(self):
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        trial_report.report(self.path, 7, NOW)
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)

    def test_an_empty_window_reports_zero_without_failing(self):
        result = trial_report.report(self.path, 1, NOW + timedelta(days=5))
        self.assertEqual(result['window']['turns'], 0)
        self.assertEqual(result['latency_seconds']['model'], {'count': 0})
        self.assertEqual(result['actions_per_turn'], {})


if __name__ == '__main__':
    unittest.main()
