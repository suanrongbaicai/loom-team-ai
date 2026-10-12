from types import SimpleNamespace
import unittest

from loom.mcp import FEATURE_TOOLS, TOOLS, available_tools

CORE = {'read', 'read_many', 'search', 'save_work', 'reply', 'record', 'case_passage', 'share_conclusion',
        'set_my_profile', 'propose', 'skill_read', 'wechat_read'}


def api(**features):
    base = {name: None for name in FEATURE_TOOLS}
    base.update(features)
    base['wechat_capture'] = SimpleNamespace(collector=features.get('collector'))
    return SimpleNamespace(**base)


class ToolCatalogTests(unittest.TestCase):
    def names(self, target, kind='private'):
        return {t['name'] for t in available_tools(target, kind)}

    def test_unprovisioned_features_are_not_offered(self):
        names = self.names(api())
        self.assertTrue(CORE <= names)
        for hidden in ('contact_private', 'send_image', 'capability_save', 'report_build', 'execute', 'image',
                       'wechat_attachment', 'wechat_capture', 'web_search', 'group_history', 'set_group_alias'):
            self.assertNotIn(hidden, names)
        self.assertLess(len(names), len(TOOLS))

    def test_a_provisioned_feature_brings_its_tools_back(self):
        names = self.names(api(contacts=object(), report_leaf=object(), collector=object()))
        self.assertTrue({'member_find', 'contact_private', 'report_build', 'cloud_copy', 'wechat_capture'} <= names)
        self.assertNotIn('send_image', names)

    def test_group_only_tools_appear_only_in_groups(self):
        self.assertNotIn('set_group_alias', self.names(api(group_history=object()), 'private'))
        self.assertTrue({'set_group_alias', 'group_history'} <= self.names(api(group_history=object()), 'group'))
        self.assertNotIn('group_history', self.names(api(), 'group'))

    def test_every_feature_tool_exists(self):
        names = {t['name'] for t in TOOLS}
        for group in FEATURE_TOOLS.values():
            self.assertTrue(set(group) <= names, group)


if __name__ == '__main__':
    unittest.main()
