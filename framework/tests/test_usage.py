from pathlib import Path
import json
import os
import tempfile
import unittest
import unittest.mock

from anc.control import Control
from anc.scheduler import Scheduler
from anc import usage
from tests.test_control import fixture


def write_events(path, usage_values, tools=2):
    lines = [{'type': 'thread.started'}]
    for i in range(tools):
        lines.append({'type': 'item.started', 'item': {'id': 'i%d' % i, 'type': 'mcp_tool_call', 'tool': 'read'}})
        lines.append({'type': 'item.completed', 'item': {'id': 'i%d' % i, 'type': 'mcp_tool_call', 'tool': 'read'}})
    lines.append({'type': 'turn.completed', 'usage': usage_values})
    path.write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')


class UsageTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.c.set_policy(fixture(self.root), 'test-policy')
        self.out = self.root / 'out'
        self.out.mkdir()
        write_events(self.out / 'events.jsonl', {'input_tokens': 1000, 'cached_input_tokens': 800, 'output_tokens': 50, 'reasoning_output_tokens': 20})
        self.job = self.root / 'job.json'
        self.home = self.root / 'codex-home'
        self.job.write_text(json.dumps({'argv': ['codex', '-m', 'example-model-a', '-c', 'model_reasoning_effort="medium"'], 'environment': {'CODEX_HOME': str(self.home)}}))
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO tasks(id,scope,actor,kind,state,epoch,created) VALUES('t1','group','alice','turn','done',1,'2026-10-03T00:00:00+00:00')")
            db.execute("INSERT INTO executions(task,job_path,output_dir,state,started,finished) VALUES('t1',?,?,'finished','2026-10-03T00:00:01+00:00','2026-10-03T00:01:00+00:00')", (str(self.job), str(self.out)))

    def tearDown(self):
        self.temp.cleanup()

    def session(self, percent=7.0, extra=None):
        folder = self.home / 'sessions' / '2026'
        folder.mkdir(parents=True, exist_ok=True)
        lines = [{'type': 'response_item', 'payload': {'type': 'message', 'content': [{'text': 'please explain rate_limits wording'}]}},
                 {'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {'primary': {'used_percent': percent, 'window_minutes': 10080, 'resets_at': 1791580425},
                                                                                          'secondary': None, 'credits': {'has_credits': True}, **(extra or {})}}}]
        (folder / 'rollout-a.jsonl').write_text('\n'.join(json.dumps(x) for x in lines) + '\n', encoding='utf-8')

    def rows(self, table):
        with self.c.db.transaction() as db:
            return [dict(r) for r in db.execute('SELECT * FROM ' + table)]

    def test_usage_is_summed_with_model_and_effort_and_is_idempotent(self):
        self.assertEqual(usage.record(self.c, 't1', self.out, self.job)['usage'], True)
        usage.record(self.c, 't1', self.out, self.job)
        rows = self.rows('usage_log')
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['input_tokens'], row['cached_tokens'], row['output_tokens'], row['reasoning_tokens'], row['tool_calls']), (1000, 800, 50, 20, 2))
        self.assertEqual((row['model'], row['effort'], row['scope'], row['actor']), ('example-model-a', 'medium', 'group', 'alice'))

    def test_quota_comes_from_the_structured_object_not_from_wording_and_is_only_stored_when_it_changes(self):
        self.session(7.0)
        self.assertTrue(usage.record(self.c, 't1', self.out, self.job, self.home)['quota'])
        self.assertFalse(usage.record(self.c, 't1', self.out, self.job, self.home)['quota'])      # unchanged: no duplicate snapshot
        snapshot = self.rows('quota_snapshots')
        self.assertEqual(len(snapshot), 1)
        self.assertEqual((snapshot[0]['used_percent'], snapshot[0]['window_minutes'], snapshot[0]['has_credits']), (7.0, 10080, 1))
        self.session(9.0)
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO tasks(id,scope,actor,kind,state,epoch,created) VALUES('t2','group','alice','turn','done',1,'2026-10-03T01:00:00+00:00')")
        usage.record(self.c, 't2', self.out, self.job, self.home)
        self.assertEqual([r['used_percent'] for r in self.rows('quota_snapshots')], [7.0, 9.0])

    def test_a_session_file_that_only_mentions_the_words_yields_no_quota(self):
        folder = self.home / 'sessions'
        folder.mkdir(parents=True)
        (folder / 'r.jsonl').write_text(json.dumps({'type': 'response_item', 'payload': {'text': 'rate_limits and used_percent: 99'}}) + '\n')
        self.assertFalse(usage.record(self.c, 't1', self.out, self.job, self.home)['quota'])
        self.assertEqual(self.rows('quota_snapshots'), [])

    @unittest.skipUnless(hasattr(Scheduler, 'record_usage'), 'collection hook is not in scheduler.py yet (held back until its turn)')
    def test_a_failure_is_audited_and_never_raises(self):
        class Row(dict):
            pass
        scheduler = Scheduler.__new__(Scheduler)
        scheduler.c = self.c
        scheduler.record_usage({'task': 't1', 'job_path': str(self.job), 'output_dir': str(self.root / 'missing-dir')})   # no events file: nothing to record, no error
        self.assertEqual(self.rows('usage_log'), [])
        with unittest.mock.patch('anc.usage.record', side_effect=RuntimeError('boom')):
            scheduler.record_usage({'task': 't1', 'job_path': str(self.job), 'output_dir': str(self.out)})
        with self.c.db.transaction() as db:
            actions = [r[0] for r in db.execute("SELECT action FROM audit WHERE task='t1'")]
        self.assertIn('usage.record_failed', actions)

    def raw_session(self, limits, name='rollout-b.jsonl'):
        folder = self.home / 'sessions' / '2026'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': limits}}) + '\n', encoding='utf-8')

    def test_short_primary_and_weekly_secondary_are_stored_per_window_so_the_weekly_one_is_not_lost(self):
        self.raw_session({'primary': {'used_percent': 40.0, 'window_minutes': 300, 'resets_at': 1791000000},
                          'secondary': {'used_percent': 12.5, 'window_minutes': 10080, 'resets_at': 1791580425}, 'credits': {'has_credits': False}})
        self.assertTrue(usage.record(self.c, 't1', self.out, self.job, self.home)['quota'])
        rows = {r['window_minutes']: r for r in self.rows('quota_snapshots')}
        self.assertEqual(sorted(rows), [300, 10080])
        self.assertEqual((rows[10080]['used_percent'], rows[10080]['resets_at']), (12.5, 1791580425))      # the weekly figure is the 10080 row
        self.assertEqual(rows[300]['used_percent'], 40.0)
        self.assertFalse(usage.record(self.c, 't1', self.out, self.job, self.home)['quota'])             # unchanged: neither window duplicated
        self.assertEqual(len(self.rows('quota_snapshots')), 2)

    def test_only_a_secondary_window_is_still_recorded(self):
        self.raw_session({'primary': None, 'secondary': {'used_percent': 3.0, 'window_minutes': 10080, 'resets_at': 5}})
        self.assertTrue(usage.record(self.c, 't1', self.out, self.job, self.home)['quota'])
        self.assertEqual([r['window_minutes'] for r in self.rows('quota_snapshots')], [10080])

    def test_the_disk_scan_runs_outside_the_control_lock(self):
        import threading
        self.session(7.0)
        seen = {}

        def probe(codex_home):
            def other():
                seen['free'] = self.c.lock.acquire(timeout=2)
                if seen['free']:
                    self.c.lock.release()
            thread = threading.Thread(target=other)
            thread.start()
            thread.join()
            return None
        with unittest.mock.patch.object(usage, 'latest_limits', probe):
            usage.record(self.c, 't1', self.out, self.job, self.home)
        self.assertTrue(seen['free'], 'latest_limits was called while the control lock was held')

    def test_session_listing_is_bounded_and_newest_first(self):
        base = self.home / 'sessions'
        for day in range(1, 41):
            folder = (base / '2026' / '09' / ('%02d' % day)) if day < 31 else (base / '2026' / '10' / ('%02d' % (day - 30)))
            folder.mkdir(parents=True, exist_ok=True)
            for n in range(5):
                (folder / ('rollout-2026-%s-%02d-0%d.jsonl' % (folder.parent.name, int(folder.name), n))).write_text('{}\n')
        stats = []
        original = Path.stat

        def counting(self, *args, **kwargs):
            stats.append(str(self))
            return original(self, *args, **kwargs)
        with unittest.mock.patch.object(Path, 'stat', counting):
            files = usage.recent_session_files(base)
        self.assertEqual(len(files), 3)
        self.assertTrue(all('/10/' in str(f) or '\\10\\' in str(f) for f in files))                     # newest month first
        self.assertEqual(stats, [])                                                                     # listing itself stats nothing
        visited = usage.recent_session_files(base, want=3, budget=1)
        self.assertEqual(visited, [])                                                                   # a tiny budget stops the walk before reaching any file

    def test_one_huge_folder_is_abandoned_while_iterating_not_after_listing_it(self):
        base = self.home / 'sessions' / '2026' / '10' / '04'
        base.mkdir(parents=True)
        for n in range(30):
            (base / ('rollout-%03d.jsonl' % n)).write_text('{}\n')
        yielded = []
        real = os.scandir

        class Counting:
            def __init__(self, folder):
                self.inner = real(folder)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.inner.close()

            def __iter__(self):
                for entry in self.inner:
                    yielded.append(entry.name)
                    yield entry
        with unittest.mock.patch('anc.usage.os.scandir', Counting):
            self.assertEqual(usage.recent_session_files(self.home / 'sessions', budget=10), [])        # more entries than the budget in ONE folder: nothing returned
        self.assertLessEqual(len(yielded), 12)                                                       # and iteration stopped at the limit (2 parents + budget), it did not read all 30
        self.assertEqual(len(usage.recent_session_files(self.home / 'sessions', budget=100)), 3)      # with room it still finds the newest three


if __name__ == '__main__':
    unittest.main()
