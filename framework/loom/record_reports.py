"""Mobile tables from existing, actually read records; reuse the report outbox."""
import json

from .control import digest, identifier
from .policy import Denied
from .references import excerpt_was_read
from .xlsx_mobile import report_xlsx


REPORT_SHAPE = ('形状：{"id":"自选ASCII编号","title":"表标题","items":[{"name":"事项名","kind":"reported|inference|recommendation|uncertain",'
                '"content":"该项内容","evidence":[{"id":"本轮read到的记录编号","revision":1,"quote":"该版本原文里连续的一句原话"}]}]}；'
                '每项 evidence 1..6 条。行列、格式、工作表由程序生成，不要传 format/sheets/columns，也不要用中文列名做键。')
ITEM_ALIASES = {'sources': 'evidence', 'source': 'evidence', 'basis': 'evidence', '依据': 'evidence', 'claim': 'content',
                'text': 'content', 'title': 'name', 'type': 'kind'}


def report_shape_problem(args):
    """Field-level shape errors before anything is read or saved. Never relaxes a rule."""
    problems = []
    missing = [k for k in ('id', 'title', 'items') if k not in args]
    if missing:
        problems.append('缺少：' + '、'.join(missing) + '。')
    unknown = sorted(set(args) - {'id', 'title', 'previous', 'items'})
    if unknown:
        problems.append('多出不属于 record_report 的字段：' + '、'.join(unknown) + '。')
    items = args.get('items')
    if isinstance(items, list):
        for n, item in enumerate(items[:100], 1):
            if not isinstance(item, dict):
                problems.append('items[%d] 必须是对象。' % n)
                break
            need = ('name', 'content', 'kind', 'evidence')
            lack = [k for k in need if k not in item]
            extra = sorted(set(item) - set(need))
            if lack or extra:
                hints = ['%s 应写作 %s' % (k, ITEM_ALIASES[k]) for k in extra if k in ITEM_ALIASES]
                problems.append('items[%d]（%s）%s%s%s' % (
                    n, str(item.get('name', ''))[:20],
                    ('缺少字段：' + '、'.join(lack) + '。') if lack else '',
                    ('多出字段：' + '、'.join(extra) + '。') if extra else '',
                    ('（' + '；'.join(hints) + '）') if hints else ''))
                break
            if item['kind'] not in ('reported', 'inference', 'recommendation', 'uncertain'):
                problems.append('items[%d] 的 kind 必须是 reported、inference、recommendation、uncertain 之一（原话陈述/分析判断/建议/待核实）。' % n)
                break
            ev = item['evidence']
            if not isinstance(ev, list) or not 1 <= len(ev) <= 6 or any(
                    not isinstance(r, dict) or set(r) != {'id', 'revision', 'quote'} for r in ev):
                problems.append('items[%d] 的 evidence 必须是 1..6 条 {id, revision, quote}，三个键都要有、不能多。' % n)
                break
    elif 'items' in args:
        problems.append('items 必须是数组。')
    return (' '.join(problems) + ' ' + REPORT_SHAPE) if problems else None


def generate(reports, ctx, request_id, title, items, previous=None):
    from .reports import LABELS
    c = reports.c
    identifier(request_id)
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 180:
        raise ValueError('Table title required, at most 180 characters')
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError('Table needs 1..100 evidence-based items')
    with c.lock:
        checked = []
        with c.db.transaction() as db:
            policy = c._check(db, ctx, True)
            for item in items:
                if (not isinstance(item, dict) or set(item) != {'name','content','kind','evidence'}
                        or item['kind'] not in LABELS
                        or not isinstance(item['name'], str) or not 1 <= len(item['name']) <= 120
                        or not isinstance(item['content'], str) or not 1 <= len(item['content']) <= 6000
                        or not isinstance(item['evidence'], list) or not 1 <= len(item['evidence']) <= 6):
                    raise ValueError('Each item needs exactly name, content, kind, evidence (1..6 sources of {id,revision,quote}); the sources key is named evidence')
                sources = []
                for ref in item['evidence']:
                    if (not isinstance(ref, dict) or set(ref) != {'id','revision','quote'}
                            or type(ref['revision']) is not int or ref['revision'] < 1
                            or not isinstance(ref['quote'], str) or not 1 <= len(ref['quote']) <= 8000):
                        raise ValueError('Evidence needs id, positive revision and exact quote')
                    from .references import read_ids_hint
                    try:
                        row, resource = c._document(db, identifier(ref['id']))
                        policy.require_read(ctx, resource)
                    except Denied:
                        raise Denied('Table evidence: 事项“%s”的依据编号不可用：%s。%s' % (item['name'][:30], ref['id'], read_ids_hint(db, ctx.task, ref['id']))) from None
                    if row['kind'] not in ('data', 'event', 'context'):
                        raise Denied('Use Data, CL or Context as table evidence')
                    raw = c.version_bytes(db, row, ref['revision'])
                    text = raw.decode('utf-8')
                    found = text.count(ref['quote'])
                    if found != 1:
                        where = '事项“%s”第%d条依据' % (item['name'][:30], item['evidence'].index(ref) + 1)
                        if found == 0:
                            raise Denied('Table evidence: %s 的 quote 在该版本原文里找不到。quote 必须是原文里连续的一段原话，不能拼接不相邻的句子、改写或漏字；同一事项有多处依据时，在该项 evidence 里分别列多条，各引一句。不要为通过校验改成标题、编号等无法说明结论的词。' % where)
                        raise Denied('Table evidence: %s 的 quote 在原文里出现 %d 处，不唯一。加长到能唯一定位的一句原话，而不是换成更泛的词。' % (where, found))
                    start = text.index(ref['quote'])
                    if not excerpt_was_read(db, ctx.task, ref['id'], ref['revision'], start, start+len(ref['quote'])):
                        raise Denied('Read the actual table evidence before using it')
                    sources.append(dict(ref, digest=digest(raw)))
                checked.append(dict(item, evidence=sources))
        ident = 'report-' + digest((ctx.scope + ':' + request_id).encode())
        old = None
        if previous:
            reports.payload(ctx, previous)
            old = json.loads(c.read(ctx, previous)['text'])
            if old.get('schema') != 'loom-record-report-v1':
                raise Denied('Previous report must be a saved record table')
        first = checked[0]['evidence'][0]
        manifest = {'schema':'loom-record-report-v1', 'id':ident, 'title':title,
                    'source':first['id'], 'source_revision':first['revision'], 'source_digest':first['digest'],
                    'family':old['family'] if old else ident, 'version':old['version']+1 if old else 1,
                    'previous':previous, 'items':checked}
        rows = [['事项','性质','内容','依据','资料版本','说明','资料更新','范围']]
        for item in checked:
            quotes = '\n\n'.join('来源'+str(n)+': '+r['quote'] for n,r in enumerate(item['evidence'],1))
            versions = '；'.join('来源'+str(n)+' / v'+str(r['revision']) for n,r in enumerate(item['evidence'],1))
            rows.append([item['name'],LABELS[item['kind']],item['content'],quotes,versions,
                         '资料陈述不等于已核实事实；备选以内容说明为准',
                         '按本次实际采用版本保存', '仅当前已获准接收人'])
        blob = report_xlsx(title, rows, labels=('事项','性质','内容','依据','资料版本','说明'),
                           date_label=None, note='事项分别列示；原文依据随项保留。事实、更正及备选由正文说明。')
        return reports._save(ctx, ident, manifest, blob)


def verify_sources(c, ctx, manifest):
    """Check every source at queue/send, including revoked or mixed audiences."""
    with c.db.transaction() as db:
        policy = c._check(db, ctx)
        items = manifest.get('items')
        if not isinstance(items, list) or not items:
            raise Denied('Saved table sources missing')
        first = items[0]['evidence'][0]
        if (first['id'],first['revision'],first['digest']) != (
                manifest.get('source'),manifest.get('source_revision'),manifest.get('source_digest')):
            raise Denied('Saved table source binding changed')
        for item in items:
            for source in item['evidence']:
                row, resource = c._document(db, source['id'])
                policy.require_read(ctx, resource)
                raw = c.version_bytes(db, row, source['revision'])
                if digest(raw) != source['digest'] or raw.decode().count(source['quote']) != 1:
                    raise Denied('Saved table evidence changed')
