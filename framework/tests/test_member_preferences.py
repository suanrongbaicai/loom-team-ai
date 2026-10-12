import json
import unittest

from loom import member_labels
from tests import test_runtime as runtime


class MemberPreferenceTests(unittest.TestCase):
    setUp = runtime.RuntimeTests.setUp
    tearDown = runtime.RuntimeTests.tearDown
    install_rules = runtime.RuntimeTests.install_rules

    def context(self, ctx, session):
        text = self.api.rules.load(ctx, 'SessionStart', session)
        return text, json.loads(text.split('## trusted current task\n', 1)[1])

    def test_only_the_speaker_receives_their_own_preferences(self):
        self.install_rules()
        bob = self.c.task('bob', 'group')
        member_labels.set_own_profile(self.c, self.ctx, '小艾', '做设计', '周报控制在三行')
        member_labels.set_own_profile(self.c, bob, None, '做销售', '群里别提我的甲项目进度')
        text, context = self.context(self.ctx, 'alice-session')
        self.assertEqual(context['speaker']['preferences'], '周报控制在三行')
        self.assertIn('speaker.preferences', context['speaker']['who_you_are_talking_to'])
        self.assertNotIn('群里别提我的甲项目进度', text)
        self.assertTrue(all('preferences' not in m for m in context['group_members']))
        self.assertIn('做销售', [m['about'] for m in context['group_members']])
        text, context = self.context(bob, 'bob-session')
        self.assertEqual(context['speaker']['preferences'], '群里别提我的甲项目进度')
        self.assertNotIn('周报控制在三行', text)

    def test_preferences_replace_as_a_whole_and_can_be_cleared(self):
        member_labels.set_own_profile(self.c, self.ctx, preferences='周报控制在三行')
        member_labels.set_own_profile(self.c, self.ctx, preferences='周报控制在三行；先说结论')
        with self.c.db.transaction() as db:
            self.assertEqual(member_labels.preferences_of(db, 'alice'), '周报控制在三行；先说结论')
            self.assertEqual(member_labels.label_of(db, 'alice'), {})
        member_labels.set_own_profile(self.c, self.ctx, preferences='')
        with self.c.db.transaction() as db:
            self.assertIsNone(member_labels.preferences_of(db, 'alice'))

    def test_about_is_kept_when_only_preferences_change(self):
        member_labels.set_own_profile(self.c, self.ctx, None, '做设计')
        member_labels.set_own_profile(self.c, self.ctx, preferences='先说结论')
        with self.c.db.transaction() as db:
            self.assertEqual(member_labels.label_of(db, 'alice')['about'], '做设计')

    def test_overlong_or_non_text_preferences_are_refused(self):
        with self.assertRaises(ValueError):
            member_labels.set_own_profile(self.c, self.ctx, preferences='长' * 501)
        with self.assertRaises(ValueError):
            member_labels.set_own_profile(self.c, self.ctx, preferences=['先说结论'])
        with self.assertRaises(ValueError):
            member_labels.set_own_profile(self.c, self.ctx)
        with self.c.db.transaction() as db:
            self.assertIsNone(member_labels.preferences_of(db, 'alice'))


if __name__ == '__main__':
    unittest.main()
