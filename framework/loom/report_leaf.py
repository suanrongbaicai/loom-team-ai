"""Evidence-backed report versions, stored as ordinary Data originals."""
import base64
import copy
import json

from .control import digest,identifier
from .policy import Denied
from .references import excerpt_was_read
from .originals import retain,find,get
from . import report_templates as templates


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


class ReportLeaf:
    def __init__(self,control,node=None,resvg=None):
        self.c,self.node,self.resvg=control,node,resvg

    def sources(self,ctx,spec,require_read=False):
        checked=copy.deepcopy(spec)
        with self.c.db.transaction() as db:
            policy=self.c._check(db,ctx)
            for row in checked['rows']:
                for ref in row['evidence']:
                    if (not isinstance(ref,dict) or set(ref)-{'id','revision','quote','digest'} or
                        type(ref.get('revision')) is not int or ref['revision']<1 or
                        not isinstance(ref.get('quote'),str) or not 1<=len(ref['quote'])<=8000):raise ValueError('Exact evidence version and quote required')
                    doc,res=self.c._document(db,identifier(ref.get('id','')));policy.require_read(ctx,res)
                    if doc['kind'] not in ('data','event','context'):raise Denied('Report evidence must be Data/CL/Context')
                    raw=self.c.version_bytes(db,doc,ref['revision']);text=raw.decode('utf-8')
                    if text.count(ref['quote'])!=1:raise Denied('Report quote must uniquely match its saved source')
                    start=text.index(ref['quote'])
                    if require_read and not excerpt_was_read(db,ctx.task,doc['id'],ref['revision'],start,start+len(ref['quote'])):raise Denied('Read report evidence before adopting it')
                    sha=digest(raw)
                    if ref.get('digest',sha)!=sha:raise Denied('Report source integrity mismatch')
                    ref['digest']=sha
        return checked

    def save(self,ctx,request_id,spec,previous=None):
        identifier(request_id);templates.validate(spec)
        with self.c.lock:
            with self.c.db.transaction() as db:self.c._check(db,ctx,True)
            checked=self.sources(ctx,spec,True)
            old=self.load(ctx,previous) if previous else None
            ident='view-'+digest((ctx.scope+':'+request_id).encode())
            manifest={'schema':'loom-report-view-v1','id':ident,'family':old['family'] if old else ident,
                      'version':old['version']+1 if old else 1,'previous':previous,'spec':checked,
                      'template_version':templates.catalog()['version'],'checks':templates.metrics(spec)}
            self.c.register(ctx,ident,'data');current=self.c.host_read(ctx,ident)
            if current['revision']:
                # A renderer upgrade must not rewrite an already saved original
                # or break an idempotent retry of its unchanged request.
                manifest['template_version']=json.loads(current['text'])['template_version']
            body=encoded(manifest).decode()
            if current['revision'] and current['text']!=body:raise Denied('Report request id already has different content')
            if not current['revision']:self.c.commit(ctx,ident,0,body,ident,self.c.lease(ctx,ident,take_over=True))
            present={get(self.c,ctx,k)[0]['part']:k for k in find(self.c,ctx,ident)}
            expected={'report-html'}
            if spec['template'] not in ('table','brief','kanban','gantt'):
                expected.update(('chart-svg','chart-svg-mobile'))
                if self.resvg:expected.add('chart-png')
            if not expected.issubset(present):
                if manifest['template_version']!=templates.catalog()['version']:
                    raise Denied('Incomplete report from an older renderer; use a new request with previous, preserving the old record')
                chart=templates.render_chart(checked,self.node,self.resvg)
                raw=templates.render(checked,chart)
                present['report-html']=retain(self.c,ctx,ident,'report-html',raw,'report.html','text/html','complete generated report')
                if chart:
                    present['chart-svg']=retain(self.c,ctx,ident,'chart-svg',chart['svg'].encode(),'chart.svg','image/svg+xml','complete generated chart')
                    present['chart-svg-mobile']=retain(self.c,ctx,ident,'chart-svg-mobile',chart['svg_mobile'].encode(),'chart-mobile.svg','image/svg+xml','complete mobile chart')
                    if chart.get('png'):present['chart-png']=retain(self.c,ctx,ident,'chart-png',base64.b64decode(chart['png']),'chart.png','image/png','complete generated chart')
            with self.c.db.transaction() as db:
                self.c._check(db,ctx,True)
                self.c.audit(db,ctx.principal,ctx.task,'report.view_saved',{'report':ident,'template':spec['template'],'version':manifest['version'],'checks':manifest['checks'],'originals':present})
            return {'report':ident,'state':'saved','version':manifest['version'],'checks':manifest['checks'],'originals':present}

    def load(self,ctx,ident):
        identifier(ident);data=json.loads(self.c.host_read(ctx,ident)['text'])
        if data.get('schema')!='loom-report-view-v1' or data.get('id')!=ident:raise Denied('Saved report view required')
        self.sources(ctx,data['spec'])
        return data

    def files(self,ctx,ident):
        self.load(ctx,ident)
        return {meta['part']:(meta,raw) for k in find(self.c,ctx,ident) for meta,raw in [get(self.c,ctx,k)]}


def sheet_values(spec):
    """Fixed stable row ids survive reordering; never convert business ids to numbers."""
    return [['_loom_row_id']+[c['label']+(' / '+c['unit'] if c.get('unit') else '') for c in spec['columns']]]+[
        [r['id']]+[r['values'][c['key']] for c in spec['columns']] for r in spec['rows']]


def sheet_diff(spec,values,expected_header=None):
    expected=sheet_values(spec);header=expected[0] if expected_header is None else expected_header
    if not isinstance(values,list) or not values or values[0]!=header:raise Denied('Sheet columns changed; preserve snapshot and resolve explicitly')
    old={r[0]:r[1:] for r in expected[1:]};seen={};changes=[]
    for row in values[1:]:
        if not row or all(v in ('',None) for v in row):continue
        rid=row[0]
        if not isinstance(rid,str) or rid not in old or rid in seen:raise Denied('Unknown or duplicate stable row id; do not guess a merge')
        cells=row[1:]+[None]*(len(header)-len(row))
        if len(cells)!=len(header)-1:raise Denied('Unexpected sheet columns')
        seen[rid]=True
        for c,before,after in zip(spec['columns'],old[rid],cells):
            if after=='':after=None
            if after is not None:
                if c['type']=='number' and (type(after) not in (int,float) or not __import__('math').isfinite(after)):raise Denied('Numeric cell became text or invalid; no silent coercion')
                if c['type'] in ('text','date') and not isinstance(after,str):raise Denied('Text identifier changed type; no silent coercion')
                if c['type']=='date':__import__('datetime').date.fromisoformat(after)
            if before!=after:changes.append({'row':rid,'column':c['key'],'before':before,'after':after})
    return {'changes':changes,'missing_rows':[r for r in old if r not in seen],'applied':False,
            'meaning':'保存差异供模型判断更正归属；不覆盖旧Data、不自动删除缺失行。'}
