"""Tests of the (not yet deployed) test-data marking, E311.  Kept apart from test_scheduler.py so the scheduler package can be deployed without it."""
import unittest

from tests.test_scheduler import SchedulerTests as _Base


class TestMarkingTests(unittest.TestCase):
    setUp = _Base.setUp
    tearDown = _Base.tearDown
    launch = _Base.launch
    context = _Base.context
    event_text = _Base.event_text

    def marked(self, task):
        with self.c.db.transaction() as db:
            return db.execute("SELECT count(*) FROM audit WHERE task=? AND action='task.test_marked'", (task,)).fetchone()[0]

    def test_test_marking_is_off_by_default(self):
        self.assertEqual(self.marked(self.event_text('tm-off', '[测试1] hi')['task']), 0)

    @unittest.expectedFailure  # E311 prototype is explicitly not deployed
    def test_test_marking_marks_only_prefixed_messages_in_listed_scopes_and_only_once(self):
        self.api.rules.features = {'test_marking': {'scopes': ['alice-private']}}
        first = self.event_text('tm-1', '[测试T9] hello')['task']
        self.assertEqual(self.marked(first), 1)
        self.assertEqual(self.marked(self.event_text('tm-2', '普通消息 [测试] 不在开头')['task']), 0)
        self.assertEqual(self.marked(self.event_text('tm-3', '测试一下')['task']), 0)
        again = self.event_text('tm-1', '[测试T9] hello')                          # the same delivery arriving again
        self.assertEqual(self.marked(first), 1)
        self.api.rules.features = {'test_marking': {'scopes': ['someone-else']}}
        self.assertEqual(self.marked(self.event_text('tm-4', '[测试T10] other scope')['task']), 0)
        with self.c.db.transaction() as db:                                          # the original message is untouched by marking
            resource = db.execute('SELECT m.resource FROM tasks t JOIN messages m ON m.id=t.event WHERE t.id=?', (first,)).fetchone()[0]
        self.assertIn('T9] hello', self.c.read(self.context(first), resource, None, 0, 4000)['text'])
