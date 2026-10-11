import unittest

from anc.rules import Rules


class InterlocutorTests(unittest.TestCase):
    def test_team_member_with_profile(self):
        text = Rules.interlocutor({'call_as': '小林', 'about': '做摄影'})
        self.assertIn('小林', text)
        self.assertIn('本企业团队成员', text)
        self.assertIn('做摄影', text)
        self.assertIn('不当作身份或权限依据', text)

    def test_unknown_field_asks_once_instead_of_guessing(self):
        text = Rules.interlocutor({'call_as': '小林'})
        self.assertIn('本企业团队成员', text)
        self.assertIn('还不知道对方具体做什么', text)

    def test_everyone_in_the_enterprise_is_a_team_member_even_with_a_legacy_false_flag(self):
        text = Rules.interlocutor({})
        self.assertIn('本企业团队成员', text)
        self.assertNotIn('尚未', text)


if __name__ == '__main__':
    unittest.main()
