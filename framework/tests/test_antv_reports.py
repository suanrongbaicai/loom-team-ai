"""Boundary and migration checks for the interactive AntV renderer."""
import copy
import json
from pathlib import Path
import subprocess
import shutil
import unittest
from unittest.mock import patch

from anc import report_templates as templates
from anc.policy import Denied
from tests.test_report_leaf import ReportLeafTests


def samples():
    def make(kind, columns, rows, view):
        return {'template':kind,'title':'AntV · '+kind+'（演示数据）',
                'subtitle':'全部为合成数据，不涉及真实业务。','columns':columns,'view':view,
                'rows':[{'id':'r'+str(i),'values':dict(zip([c['key'] for c in columns],r)),
                         'evidence':[{'id':'synthetic-demo','revision':1,'quote':'合成数据'}]} for i,r in enumerate(rows)]}
    text=lambda k,n:{'key':k,'label':n,'type':'text'}
    num=lambda k,n,u='元':{'key':k,'label':n,'type':'number','unit':u}
    day=lambda k,n:{'key':k,'label':n,'type':'date'}
    result=[]
    for kind,rows in [('comparison',[['青禾',88],['丰收',30],['零值',0],['待定',None]]),
                      ('composition',[['门店',88],['线上',30],['其它',12]]),
                      ('waterfall',[['期初',100],['收入',35],['支出',-42]])]:
        result.append(make(kind,[text('name','项目'),num('value','金额')],rows,{'label':'name','value':'value'}))
    result.append(make('trend',[day('date','日期'),num('value','金额')],
        [['2026-09-21',30],['2026-09-22',0],['2026-09-23',None],['2026-09-24',88],['2026-09-25',73]],{'x':'date','value':'value'}))
    result.append(make('distribution',[num('value','金额')],[[x] for x in [0,12,23,23,30,30,35,66,88,None]],{'value':'value'}))
    result.append(make('relationship',[text('name','项目'),num('x','次数','次'),num('y','金额')],
        [['青禾',3,88],['丰收',5,30],['零值',0,0],['待定',2,None]],{'label':'name','x':'x','y':'y'}))
    result.append(make('matrix',[text('a','城市'),text('b','品类'),num('value','金额')],
        [['杭州','甲',88],['杭州','乙',30],['上海','甲',0],['上海','乙',None]],{'x':'a','y':'b','value':'value'}))
    result.append(make('candlestick',[day('date','日期'),num('open','开盘'),num('high','最高'),num('low','最低'),num('close','收盘')],
        [['2026-09-21',30,40,25,35],['2026-09-22',35,42,20,26],['2026-09-23',28,38,22,28]],
        {'x':'date','open':'open','high':'high','low':'low','close':'close'}))
    result.append(make('gantt',[text('name','任务'),day('start','开始'),day('end','结束')],
        [['准备','2026-09-21','2026-09-23'],['试用','2026-09-24','2026-09-25']],{'label':'name','start':'start','end':'end'}))
    return result


class AntVTests(unittest.TestCase):
    def test_waterfall_crossing_zero_keeps_actual_bars_in_mobile_export(self):
        node=shutil.which('node') or r'<configured-node-executable>'
        if not Path(node).exists():self.skipTest('Trusted Node unavailable')
        spec=next(s for s in samples() if s['template']=='waterfall')
        spec['rows'][0]['values']['value']=-100
        chart=templates.render_chart(spec,node)
        for key in ('svg','svg_mobile'):
            self.assertIn('fill="#d63939"',chart[key])
            self.assertIn('fill="#066fd1"',chart[key])

    def test_offline_chart_and_untrusted_text_are_preserved_without_script_injection(self):
        for spec in samples():
            spec=copy.deepcopy(spec)
            spec['title']='</script><img src=x onerror=alert(1)>'
            content=templates.render(spec).decode()
            self.assertNotIn(spec['title'],content)
            self.assertIn('connect-src \'none\'',content)
            self.assertIn('ANCChart.mount',content)
            self.assertNotIn('<script src=',content)
            self.assertNotIn('new Gantt',content)


class AntVMigrationTests(ReportLeafTests):
    # Reuse setup only: run this named migration test without the inherited suite.
    def test_saved_report_retry_survives_template_upgrade_without_rewriting_original(self):
        old=templates.catalog()
        with patch.object(templates,'catalog',return_value={**old,'version':'old-renderer'}):
            report=self.setup_report()
        before=self.leaf.files(self.a,report)
        retry=self.leaf.save(self.a,'sample',self.spec)
        self.assertEqual(retry['report'],report)
        self.assertEqual(self.leaf.files(self.a,report),before)
        fresh=self.leaf.save(self.a,'new-renderer',self.spec,previous=report)
        self.assertEqual(self.leaf.load(self.a,fresh['report'])['template_version'],old['version'])


if __name__=='__main__':unittest.main()
