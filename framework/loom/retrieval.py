"""Small rebuildable lexical index. Meaning and object identity stay model-owned."""
import hashlib
import json
import re
import sqlite3
import unicodedata

from .policy import Denied, Resource


def normalized(text):
    return ''.join(unicodedata.normalize('NFKC', text).casefold().split())


def grams(text):
    # Encoded character grams support one/two-character Chinese and substrings.
    # They select candidates only; original normalized phrases are checked next.
    terms = {'u' + format(ord(c), 'x') for c in text}
    terms.update('b' + format(ord(a), 'x') + 'z' + format(ord(b), 'x')
                 for a, b in zip(text, text[1:]))
    return sorted(terms)


def query_parts(query=None, queries=None):
    if queries is not None and not isinstance(queries, list):
        raise ValueError('queries must be a list')
    items = ([query] if query is not None else []) + (queries or [])
    if not items or len(items) > 8 or not all(isinstance(x, str) and 0 < len(x.strip()) <= 256 for x in items):
        raise ValueError('Provide 1..8 nonempty query wordings, each at most 256 characters')
    result = []
    for item in items:
        words = list(dict.fromkeys(normalized(word) for word in item.split()))
        if not 1 <= len(words) <= 12:
            raise ValueError('At most 12 words per query')
        result.append(words)
    return result


def metadata(text):
    """Read our optional, model-written Context header, never infer identity."""
    if not text.startswith('---\nloom_lookup: ') or '\n---\n' not in text[4:]:
        return None
    try:
        item = json.loads(text.splitlines()[1][len('loom_lookup: '):])
        if item.get('schema') != 1 or not isinstance(item.get('name'), str):
            return None
        if not isinstance(item.get('aliases'), list) or not all(isinstance(a, str) for a in item['aliases']):
            return None
        if (not isinstance(item.get('related',[]),list) or not all(isinstance(v,str) for v in item.get('related',[]))
                or not isinstance(item.get('type',''),str)):
            return None
        return item
    except (ValueError, IndexError, AttributeError):
        return None


def title(text, fallback):
    info = metadata(text)
    if info:
        return info['name'][:120]
    for line in text.splitlines()[:12]:
        if line.strip():
            return line.strip().lstrip('# ').strip()[:120]
    return fallback


class Index:
    """Process-local SQLite, 48 MiB input budget (text plus encoded grams).

    No second durable knowledge store, schema migration or background service.
    Every query verifies current permitted originals through the existing reader.
    If the bound is reached the remaining sources are searched directly.
    """
    def __init__(self):
        self.db = sqlite3.connect(':memory:', check_same_thread=False)
        try:
            self.db.execute('CREATE TABLE docs(id TEXT PRIMARY KEY,revision INTEGER,digest TEXT,body TEXT,size INTEGER)')
            self.db.execute("CREATE VIRTUAL TABLE words USING fts5(id UNINDEXED,terms,tokenize='unicode61')")
        except sqlite3.Error:
            self.db.close();raise
        self.bytes = 0
        self.max_bytes = 48 * 1024 * 1024

    def __del__(self):
        if hasattr(self,'db'):self.db.close()

    def put(self, row, body):
        old = self.db.execute('SELECT revision,digest,size FROM docs WHERE id=?', (row['id'],)).fetchone()
        if old and old[:2] == (row['revision'], row['digest']):
            return True
        if old:
            self.db.execute('DELETE FROM docs WHERE id=?', (row['id'],))
            self.db.execute('DELETE FROM words WHERE id=?', (row['id'],))
            self.bytes -= old[2]
        size = len(body.encode('utf-8'))
        # Large originals still work; do not let a single source build a huge FTS.
        if size > 512 * 1024 or self.bytes + size > self.max_bytes:
            return False
        encoded = ' '.join(grams(body))
        if len(encoded) > 2 * 1024 * 1024:
            return False
        size += len(encoded)
        if self.bytes + size > self.max_bytes:
            return False
        self.db.execute('INSERT INTO docs VALUES(?,?,?,?,?)', (row['id'],row['revision'],row['digest'],body,size))
        self.db.execute('INSERT INTO words VALUES(?,?)', (row['id'],encoded))
        self.db.commit()
        self.bytes += size
        return True

    def candidates(self, groups):
        expressions = []
        for words in groups:
            tokens = set()
            for word in words:
                # Bigrams imply their constituent unigrams; retain one-char words.
                tokens.update(t for t in grams(word) if len(word) == 1 or t.startswith('b'))
            expressions.append('(' + ' AND '.join('"'+t+'"' for t in sorted(tokens)) + ')')
        expression = ' OR '.join(expressions)
        return {r[0] for r in self.db.execute('SELECT id FROM words WHERE words MATCH ?', (expression,))}


def source_rows(c, db, policy, ctx, kinds=None):
    result = []
    for row in db.execute('SELECT * FROM documents WHERE revision>0 ORDER BY id'):
        resource = Resource(row['id'],row['owner'],row['scope'],row['kind'],row['created'],tuple(json.loads(row['audience'])))
        if policy.can_read(ctx, resource) and (not kinds or row['kind'] in kinds):
            result.append(row)
    return result


def page_check(rows, ctx, options, offset, snapshot):
    if type(offset) is not int or offset < 0:
        raise ValueError('Invalid page offset')
    body = [ctx.principal, ctx.scope, ctx.epoch, options,
            [(r['id'],r['revision'],r['digest']) for r in rows]]
    current = hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if (offset and snapshot is None) or (snapshot is not None and snapshot != current):
        raise Denied('资料或查询范围已变化，请从第一页重新查；不能沿旧分页声称完整。')
    return current


def excerpts(text, groups, width=900):
    """Small exact original ranges. Never return reconstructed text as a quote."""
    lines = text.splitlines(keepends=True)
    offsets, at = [], 0
    for line in lines:
        offsets.append(at); at += len(line)
    selected = []
    needles = list(dict.fromkeys(w for group in groups for w in group))
    for i, line in enumerate(lines):
        plain = normalized(line)
        if not any(word in plain for word in needles):
            continue
        start = offsets[max(0, i-1)]
        end = offsets[i] + len(line)
        if i+1 < len(lines):
            end += len(lines[i+1])
        if end-start > width:
            # Locate a hit even in a long JSON line, preserving source offsets.
            mapping, norm = [], []
            for j, ch in enumerate(line):
                for folded in unicodedata.normalize('NFKC',ch).casefold():
                    if not folded.isspace(): norm.append(folded); mapping.append(j)
            compact = ''.join(norm)
            hits = [compact.find(word) for word in needles if word in compact]
            center = offsets[i] + (mapping[min(hits)] if hits else 0)
            start = max(offsets[i], center-180)
            end = min(len(text), start+width)
        if any(a['start'] <= start and end <= a['end'] for a in selected):
            continue
        selected.append({'start':start,'end':end,'start_line':text[:start].count('\n')+1,
                         'end_line':text[:end].count('\n')+1,'text':text[start:end]})
        if len(selected) == 3:
            break
    if not selected and text:
        selected=[{'start':0,'end':min(len(text),width),'start_line':1,
                   'end_line':text[:width].count('\n')+1,'text':text[:width]}]
    return selected


def search(c, ctx, query=None, limit=20, queries=None, kinds=None, offset=0, snapshot=None):
    groups = query_parts(query, queries)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be 1..100')
    if kinds is not None and (not isinstance(kinds,list) or not kinds or any(k not in ('data','event','context','reply') for k in kinds)):
        raise ValueError('Invalid record kinds')
    with c.lock, c.db.transaction() as db:
        policy = c._check(db,ctx)
        rows = source_rows(c,db,policy,ctx,kinds)
        snap = page_check(rows,ctx,{'queries':groups,'kinds':kinds},offset,snapshot)
        if not hasattr(c,'_search_index'):
            try: c._search_index = Index()
            except sqlite3.Error: c._search_index = None
        index = c._search_index
        sources, fallback = {}, set()
        for row in rows:
            text = c._read(db,row).decode('utf-8')
            body = normalized(text)
            sources[row['id']] = (row,text,body)
            try:
                if index is None or not index.put(row,body): fallback.add(row['id'])
            except sqlite3.Error:
                index.db.close();c._search_index=None;index=None
        try:
            candidates = (index.candidates(groups) | fallback) if index else set(sources)
        except sqlite3.Error:
            index.db.close();c._search_index=None;candidates=set(sources)
        found = []
        for key in candidates:
            if key not in sources: continue  # Filter before scoring, counts, snippets.
            row,text,body = sources[key]
            matches = [words for words in groups if all(word in body for word in words)]
            if not matches: continue
            heading = title(text,key)
            score = 10*len(matches) + sum(5 for words in matches for word in words if word in normalized(heading))
            score += sum(3 for words in matches if ''.join(words) in body)
            found.append((score,row,text,heading,matches))
        found.sort(key=lambda x:(-x[0],x[1]['id']))
        results, receipts = [], []
        for score,row,text,heading,matches in found[offset:offset+limit]:
            spans = excerpts(text,matches)
            display='\n…\n'.join(s['text'] for s in spans)
            located=[];at=0
            for span in spans:
                located.append({k:v for k,v in span.items() if k!='text'} |
                               {'result_start':at,'result_end':at+len(span['text'])})
                at+=len(span['text'])+3
            results.append({'id':row['id'],'kind':row['kind'],'revision':row['revision'],
                            'title':heading,'text':display,
                            'excerpts':located,'full_text':False,'score':score})
            receipts.append({'id':row['id'],'revision':row['revision'],
                             'ranges':[[s['start'],s['end']] for s in spans]})
            db.execute('INSERT OR IGNORE INTO task_reads VALUES(?,?)',(ctx.task,row['id']))
        more = offset+len(results) < len(found)
        c.audit(db,ctx.principal,ctx.task,'document.search',
                {'queries':groups,'readable_files':len(rows),'matches':len(found),'returned':receipts,
                 'partial':True,'truncated':more,'offset':offset,'snapshot':snap})
        return {'results':results,'matches':len(found),'readable_files':len(rows),'truncated':more,
                'next_offset':offset+len(results) if more else None,'snapshot':snap,
                'coverage':'当前范围全部已登记可读文字记录；只返回明确片段，未解析附件正文不在覆盖内。',
                'method':'local lexical index + exact normalized words; no semantic identity inference'}


def objects(c,ctx,query=None,limit=30,offset=0,snapshot=None):
    from .lookup import recheck
    if type(limit) is not int or not 1<=limit<=100: raise ValueError('limit must be 1..100')
    if query is not None and (not isinstance(query,str) or not query.strip() or len(query)>256):
        raise ValueError('Invalid object query')
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx);rows=source_rows(c,db,policy,ctx,['context'])
        snap=page_check(rows,ctx,{'objects':query},offset,snapshot)
        available={r['id'] for r in source_rows(c,db,policy,ctx)}
        entries=[];unregistered=0
        for row in rows:
            text=c._read(db,row).decode();item=metadata(text)
            if not item: unregistered+=1;continue
            names=[item['name'],*item['aliases']]
            q=normalized(query) if query is not None else None
            exact=q is not None and any(q==normalized(n) for n in names)
            if q is not None and not any(q in normalized(n) for n in names) and q!=normalized(row['id']):continue
            entry={'id':row['id'],'revision':row['revision'],'name':item['name'],
                   'aliases':item['aliases'],'type':item.get('type',''),
                   'related':[key for key in item.get('related',[]) if key in available],
                   'match':'exact_name_or_alias' if exact else 'candidate',
                   'authority':'model-recorded business correspondence; not identity or permission'}
            status=recheck(item)
            if status:
                entry['recheck']=status
            entries.append(entry)
        entries.sort(key=lambda v:(v['match']!='exact_name_or_alias',v['id']))
        page=entries[offset:offset+limit];more=offset+len(page)<len(entries)
        c.audit(db,ctx.principal,ctx.task,'objects.located',{'query':query,'returned':[x['id'] for x in page],
                 'matches':len(entries),'snapshot':snap,'offset':offset})
        for item in page:db.execute('INSERT OR IGNORE INTO task_reads VALUES(?,?)',(ctx.task,item['id']))
        return {'entries':page,'matches':len(entries),'ambiguous':len(entries)>1,'snapshot':snap,
                'next_offset':offset+len(page) if more else None,'truncated':more,
                'unstructured_contexts':unregistered,
                'coverage':'只定位模型显式登记的对象；未登记的旧资料继续用search/navigation，空结果不证明对象不存在。'}
