"""Fixed report slots. Business meaning stays with the model; no generated code."""
from datetime import date
from decimal import Decimal
import html
import json
import math
import os
from pathlib import Path
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]/'templates'/'reports'


def catalog():
    return json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))


def validate(spec):
    if not isinstance(spec,dict) or set(spec)-{'template','title','subtitle','summary','columns','rows','view','totals'}:
        raise ValueError('Report uses fixed named slots only')
    templates={t['id']:t for t in catalog()['templates']}
    if spec.get('template') not in templates:raise ValueError('Unknown report template')
    for key,limit,required in [('title',160,True),('subtitle',240,False),('summary',1800,False)]:
        value=spec.get(key,'')
        if not isinstance(value,str) or len(value)>limit or (required and not value.strip()):raise ValueError('Invalid '+key)
    columns=spec.get('columns');rows=spec.get('rows');view=spec.get('view',{})
    if not isinstance(columns,list) or not 1<=len(columns)<=16:raise ValueError('Use 1..16 explicitly typed columns')
    if not isinstance(rows,list) or not 1<=len(rows)<=500:raise ValueError('Use 1..500 rows; split larger reports explicitly')
    keys={}
    for col in columns:
        if (not isinstance(col,dict) or set(col)-{'key','label','type','unit'} or
                not re.fullmatch(r'[a-z][a-z0-9_]{0,39}',col.get('key','')) or col['key'] in keys or
                col.get('type') not in ('text','number','date') or not isinstance(col.get('label'),str) or
                not 1<=len(col['label'])<=60 or not isinstance(col.get('unit',''),str) or len(col.get('unit',''))>20):
            raise ValueError('Invalid or duplicate typed column')
        keys[col['key']]=col
    rowids=set()
    for row in rows:
        if (not isinstance(row,dict) or set(row)-{'id','values','evidence','note'} or
                not isinstance(row.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',row['id']) or row['id'] in rowids or
                not isinstance(row.get('values'),dict) or set(row['values'])!=set(keys) or
                not isinstance(row.get('evidence'),list) or not 1<=len(row['evidence'])<=6 or
                not isinstance(row.get('note',''),str) or len(row.get('note',''))>800):raise ValueError('Each row needs stable id, all values and 1..6 sources')
        rowids.add(row['id'])
        for key,value in row['values'].items():
            if value is None:continue
            kind=keys[key]['type']
            if kind=='number':
                if type(value) not in (int,float) or not math.isfinite(value) or abs(value)>1e14:raise ValueError('Number must be finite and within exact presentation range')
            elif not isinstance(value,str) or len(value)>4000:raise ValueError('Text identifiers must stay text')
            elif kind=='date':date.fromisoformat(value)
    required=templates[spec['template']]['slots']
    if not isinstance(view,dict) or set(view)!=set(required):raise ValueError('view must map exactly: '+','.join(required))
    for slot,key in view.items():
        if key not in keys:raise ValueError('Unknown view column')
        numeric=slot in ('open','close','low','high') or (slot in ('value','y','x') and
                spec['template'] not in ('table','brief','kanban','gantt') and
                not (slot=='x' and spec['template'] in ('trend','candlestick','matrix')) and
                not (slot=='y' and spec['template']=='matrix'))
        if numeric and keys[key]['type']!='number':raise ValueError('Chart measure must be numeric: '+slot)
        if slot in ('start','end') and keys[key]['type']!='date':raise ValueError('Schedule needs ISO dates')
    if spec['template']=='trend':
        if keys[view['x']]['type']!='date':raise ValueError('Trend x must be an actual date')
        dates=[r['values'][view['x']] for r in rows]
        if None in dates or len(set(dates))!=len(dates):raise ValueError('Trend needs unique observed dates')
    if spec['template']=='gantt':
        for r in rows:
            start,end=(r['values'][view[k]] for k in ('start','end'))
            if start is None or end is None or end<start:raise ValueError('Invalid schedule date range')
    if spec['template']=='composition':
        values=[r['values'][view['value']] for r in rows]
        if len(rows)>8 or any(v is None or v<0 for v in values) or sum(values)<=0:raise ValueError('Composition needs <=8 nonnegative complete parts with positive total')
    if spec['template']=='candlestick':
        dates=[r['values'][view['x']] for r in rows]
        if keys[view['x']]['type']!='date' or None in dates or len(set(dates))!=len(dates):raise ValueError('OHLC needs unique actual dates')
        for r in rows:
            o,c,lo,hi=(r['values'][view[k]] for k in ('open','close','low','high'))
            if any(v is None for v in (o,c,lo,hi)) or not lo<=min(o,c)<=max(o,c)<=hi:raise ValueError('OHLC range is inconsistent')
    if spec['template']=='waterfall' and any(r['values'][view['value']] is None for r in rows):raise ValueError('Waterfall cannot replace missing changes with zero')
    if spec['template']=='matrix':
        pairs=[(r['values'][view['x']],r['values'][view['y']]) for r in rows]
        if any(None in p for p in pairs) or len(set(pairs))!=len(pairs):raise ValueError('Matrix needs unique nonempty axis pairs; aggregate explicitly')
        if all(r['values'][view['value']] is None for r in rows):raise ValueError('Matrix has no observed values')
    totals=spec.get('totals',[])
    if not isinstance(totals,list) or len(totals)>4 or len(set(totals))!=len(totals) or any(k not in keys or keys[k]['type']!='number' for k in totals):raise ValueError('Totals must identify up to four numeric columns')
    if len(json.dumps(spec,ensure_ascii=False).encode())>600000:raise ValueError('Report too large; split it')
    return spec


def metrics(spec):
    result={'rows':len(spec['rows']),'totals':{}}
    for key in spec.get('totals',[]):
        values=[r['values'][key] for r in spec['rows']]
        good=[v for v in values if v is not None]
        result['totals'][key]={'sum':str(sum((Decimal(str(v)) for v in good),Decimal(0))),
                              'known':len(good),'missing':len(values)-len(good)}
    return result


def render_chart(spec,node=None,resvg=None):
    if spec['template'] in ('table','brief','kanban','gantt'):return None
    if not node:raise ValueError('Trusted chart renderer is not configured')
    env=dict(os.environ)
    env.pop('LOOM_RESVG_MODULE',None)
    if resvg:env['LOOM_RESVG_MODULE']=str(resvg)
    result=subprocess.run([str(node),str(ROOT/'render-chart.cjs')],input=json.dumps(spec,ensure_ascii=False).encode(),env=env,
                          capture_output=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError('Fixed chart renderer failed: '+result.stderr.decode(errors='replace')[:240])
    return json.loads(result.stdout)


def shown(value):
    return '未提供' if value is None else str(value)


def render(spec,chart=None):
    validate(spec);e=lambda x:html.escape(shown(x),quote=True)
    cols=spec['columns'];rows=spec['rows'];view=spec.get('view',{});kind=spec['template'];m=metrics(spec)
    css=(ROOT/'vendor'/'tabler.min.css').read_text(encoding='utf-8')
    # Tabler's original component CSS is retained; system fonts avoid remote font calls.
    css=re.sub(r'/\*# sourceMappingURL=.*?\*/','',css)
    extra=(ROOT/'loom-theme.css').read_text(encoding='utf-8')
    pieces=[f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; img-src data:; font-src data:; connect-src \'none\'"><title>{e(spec["title"])}</title><style>{css}\n{extra}</style></head><body class="loom"><main>',
            f'<header class="report-head"><div class="page-pretitle mb-2">Loom</div><h1>{e(spec["title"])}</h1><p class="text-secondary">{e(spec.get("subtitle",""))}</p><p class="report-summary note">{e(spec.get("summary",""))}</p></header>']
    if m['totals']:
        pieces.append('<div class="report-grid">')
        for key,total in m['totals'].items():
            col=next(c for c in cols if c['key']==key)
            pieces.append(f'<div class="card"><div class="card-body"><div class="subheader">{e(col["label"])} · 已知值合计</div><div class="stat-value my-2">{e(total["sum"])} <span class="fs-4 text-secondary">{e(col.get("unit",""))}</span></div><div class="text-secondary">{total["known"]} 条已知，{total["missing"]} 条未提供</div></div></div>')
        pieces.append('</div>')
    if kind in ('comparison','trend','composition','distribution','relationship','matrix','waterfall','candlestick','gantt'):
        # Ship the pinned library with the retained HTML. It works without a CDN
        # and cannot send report content to an external rendering service.
        library=(ROOT/'vendor'/'antv-g2-5.4.8.min.js').read_text(encoding='utf-8')
        adapter=(ROOT/'antv-report.js').read_text(encoding='utf-8')
        payload=json.dumps(spec,ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
        pieces.append('<section class="card"><div class="card-header"><h2 class="card-title">'+e(spec['title'])+'</h2></div><div class="card-body"><div id="antv-chart" role="figure" aria-label="'+e(spec['title'])+'" style="width:100%;min-height:340px;position:relative"></div><p class="text-secondary small mt-2 mb-0">悬停或点按图中数据查看数值；点按分类图例筛选。未提供的数值不按零计算，完整记录见明细。</p><p id="chart-error" role="status" hidden>交互图未能载入，请查看下方明细。</p></div></section>')
        pieces.append('<script>'+library.replace('</script','<\\/script')+'</script><script>'+adapter+'</script><script>LOOMChart.mount(document.getElementById("antv-chart"),'+payload+').catch(function(){document.getElementById("chart-error").hidden=false;});</script>')
    if kind=='kanban':
        states=list(dict.fromkeys(shown(r['values'][view['status']]) for r in rows));pieces.append('<div class="board">')
        for state in states:
            selected=[r for r in rows if shown(r['values'][view['status']])==state]
            pieces.append(f'<section class="board-column"><h2 class="h3 mb-3">{e(state)} <span class="badge bg-secondary-lt">{len(selected)}</span></h2>')
            for row in selected:
                pieces.append(f'<div class="card"><div class="progress card-progress"><div class="progress-bar bg-blue" style="width:100%"></div></div><div class="card-body"><h3 class="card-title">{e(row["values"][view["label"]])}</h3>')
                for col in cols:
                    if col['key'] not in (view['label'],view['status']):pieces.append(f'<div class="kv"><span>{e(col["label"])}</span><strong>{e(row["values"][col["key"]])}</strong></div>')
                pieces.append(f'<p class="text-secondary mt-3 mb-0 note">{e(row.get("note",""))}</p></div></div>')
            pieces.append('</section>')
        pieces.append('</div>')
    if kind=='brief':
        for row in rows:pieces.append(f'<section class="card"><div class="card-body"><h2 class="card-title">{e(row["values"][view["label"]])}</h2><p class="note mb-0">{e(row["values"][view["value"]])}</p></div></section>')
    if kind not in ('kanban','brief'):
        head=''.join('<th'+(' class="num"' if c['type']=='number' else '')+'>'+e(c['label'])+(' / '+e(c['unit']) if c.get('unit') else '')+'</th>' for c in cols)
        body=''.join('<tr>'+''.join('<td'+(' class="num"' if c['type']=='number' else '')+'>'+e(r['values'][c['key']])+'</td>' for c in cols)+'</tr>' for r in rows)
        pieces.append(f'<section class="card desktop-table"><div class="card-header"><h2 class="card-title">明细 <span class="text-secondary">{len(rows)} 条</span></h2></div><div class="table-responsive"><table class="table table-vcenter card-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></section><section class="mobile-rows">')
        for row in rows:
            pieces.append('<article class="card"><div class="card-body">')
            for i,col in enumerate(cols):
                val=e(row['values'][col['key']]);label=e(col['label']);unit=e(col.get('unit',''))
                pieces.append(f'<h2 class="card-title">{val}</h2>' if i==0 else f'<div class="kv"><span>{label}</span><strong>{val}{(" "+unit) if unit and row["values"][col["key"]] is not None else ""}</strong></div>')
            pieces.append('</div></article>')
        pieces.append('</section>')
    pieces.append('<details class="card"><summary class="card-header">查看资料依据</summary><div class="card-body report-source">')
    for row in rows:
        pieces.append('<p><strong>'+e(row['id'])+'</strong> '+e(row.get('note',''))+'</p>')
        for ref in row['evidence']:pieces.append('<p>'+e(ref['id'])+' · v'+e(ref['revision'])+'<br>'+e(ref['quote'])+'</p>')
    pieces.append('</div></details><footer class="text-secondary small py-3">按本次已取得资料生成 · 原件及版本保存在 Loom · 交互图：AntV G2 · 排版：Tabler</footer></main></body></html>')
    return ''.join(pieces).encode('utf-8')
