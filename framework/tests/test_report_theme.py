import re
import unittest

from anc import report_templates as templates
from tests.test_antv_reports import samples


def table_spec():
    cols = [{'key': 'name', 'label': '客户', 'type': 'text'}, {'key': 'amt', 'label': '金额', 'type': 'number', 'unit': '元'}]
    rows = [{'id': 'r%d' % i, 'values': {'name': n, 'amt': a}, 'evidence': [{'id': 'd', 'revision': 1, 'quote': 'q'}]}
            for i, (n, a) in enumerate([('青禾', 88), ('待定', None)])]
    return {'template': 'table', 'title': '客户明细', 'columns': cols, 'rows': rows, 'view': {}, 'totals': ['amt']}


class ThemeTests(unittest.TestCase):
    def test_every_template_carries_the_shared_theme_light_dark_and_mobile_rules(self):
        for spec in samples() + [table_spec()]:
            page = templates.render(spec).decode()
            self.assertIn('<body class="anc">', page, spec['template'])
            self.assertIn('prefers-color-scheme:dark', page)
            self.assertIn('max-width:640px', page)
            self.assertIn('--s1:#2a78d6', page)

    def test_numbers_align_right_and_a_missing_value_gets_no_unit(self):
        page = templates.render(table_spec()).decode()
        self.assertIn('<th class="num">金额 / 元</th>', page)
        self.assertIn('<td class="num">88</td>', page)
        self.assertIn('<td class="num">未提供</td>', page)
        self.assertIn('<strong>88 元</strong>', page)
        self.assertIn('<strong>未提供</strong>', page)
        self.assertNotIn('未提供 元', page)

    def test_the_chart_adapter_reads_its_colours_from_the_theme(self):
        script = (templates.ROOT / 'antv-report.js').read_text(encoding='utf-8')
        self.assertIn("cssVar('--s1'", script)
        self.assertNotIn('#5B8FF9', script)
        css = (templates.ROOT / 'anc-theme.css').read_text(encoding='utf-8')
        for token in ('--s1', '--s2', '--s3', '--ink', '--up', '--down'):
            self.assertIn(token, css)

    def test_no_remote_resources_are_referenced(self):
        css = (templates.ROOT / 'anc-theme.css').read_text(encoding='utf-8')
        self.assertFalse(re.search(r'url\(|@import|https?://', css))


if __name__ == '__main__':
    unittest.main()
