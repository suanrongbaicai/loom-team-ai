"""Model-chosen passage citations, exact versions, and a rebuildable receipt index."""
import json
import os
from urllib.parse import quote

from .control import digest, identifier, write_durable
from .policy import Denied


def read_versions(db, task, key):
    versions = []
    for row in db.execute("SELECT action,detail FROM audit WHERE task=? AND action IN ('document.read','document.search') ORDER BY id", (task,)):
        detail = json.loads(row['detail'])
        if row['action'] == 'document.read' and detail.get('resource') == key:
            versions.append(detail['revision'])
        elif row['action'] == 'document.search':
            versions.extend(r['revision'] for r in detail.get('returned',[]) if r['id'] == key)
    return versions


def excerpt_was_read(db, task, key, revision, start, end):
    """A snippet/partial read cannot certify unseen parts of the same version."""
    ranges=[]
    for row in db.execute("SELECT action,detail FROM audit WHERE task=? AND action IN ('document.read','document.search') ORDER BY id",(task,)):
        detail=json.loads(row['detail'])
        if row['action']=='document.read':
            if (detail.get('resource'),detail.get('revision'))!=(key,revision):continue
            if 'ranges' not in detail:return True  # Legacy read returned full text.
            ranges.extend(detail['ranges'])
        else:
            for item in detail.get('returned',[]):
                if (item['id'],item['revision'])!=(key,revision):continue
                if not detail.get('partial'):return True  # Legacy search full body.
                ranges.extend(item.get('ranges',[]))
    cursor=start
    for a,b in sorted(ranges):
        if a<=cursor:cursor=max(cursor,b)
        if cursor>=end:return True
    return False


def read_ids_hint(db, task, key):
    """After a mistyped/pasted-together record id: list ids THIS task actually read (it already knows them). Says nothing
    about whether the wrong id exists or who may see it."""
    rows = [r[0] for r in db.execute('SELECT document FROM task_reads WHERE task=? ORDER BY rowid', (task,))]
    near = [d for d in rows if d[:16] == str(key)[:16]] or rows[-5:]
    return '你本轮读到的记录编号有：' + '、'.join(near[:5]) + '。编号要原样复制读回结果里的，不要手抄或拼接。' if near else '本轮还没有读到任何记录，请先 read。'


def passage(c, ctx, event, base, text, commit_id, citations, take_over=False):
    """Append one actual case passage. Retry identity is the normal commit ID."""
    identifier(event); identifier(commit_id)
    if not isinstance(text,str) or not text.strip() or '<!-- anc-citations:' in text:
        raise ValueError('One nonempty passage required')
    if not isinstance(citations,list) or len(citations)>40:
        raise ValueError('At most 40 actual adoptions per passage')
    with c.lock:
        with c.db.transaction() as db:
            policy=c._check(db,ctx,True)
            existing=db.execute('SELECT * FROM documents WHERE id=?',(event,)).fetchone()
            if existing:
                _,known=c._document(db,event)
                if policy.can_read(ctx,known) and existing['kind']=='event' and existing['scope']!=ctx.scope:
                    raise Denied('该CL当前可读但不可写：'+event+'。请在当前范围建立或接续可写CL，并引用此旧CL，不能重复尝试改写它。')
        c.register(ctx,event,'event')
        with c.db.transaction() as db:
            policy=c._check(db,ctx,True)
            target,resource=c._document(db,event);policy.require_write(ctx,resource)
            anchor='use-'+commit_id
            adopted=[];links=[]
            for n,item in enumerate(citations):
                key=identifier(item['id']);revision=item['revision'];excerpt=item['quote']
                try:
                    row,source=c._document(db,key);policy.require_read(ctx,source)
                except Denied:
                    raise Denied('引用的记录编号不可用：'+key+'。'+read_ids_hint(db,ctx.task,key)) from None
                if revision not in read_versions(db,ctx.task,key):
                    raise Denied('引用版本须在本轮实际读取；先用 read(id,revision) 核对。')
                raw=c.version_bytes(db,row,revision);body=raw.decode()
                if not isinstance(excerpt,str) or not excerpt or len(excerpt)>8000 or body.count(excerpt)!=1:
                    raise Denied('引用段落须唯一匹配已读原文；提供足以定位的短原文。')
                begin=body.index(excerpt);start=body[:begin].count('\n')+1
                if not excerpt_was_read(db,ctx.task,key,revision,begin,begin+len(excerpt)):
                    raise Denied('该原文片段尚未实际读到；搜索和分段读取不代表读完全文，请补读所需范围。')
                end=start+excerpt.count('\n')
                version=c._history(c._path(db,row),revision)
                if version.exists():
                    if version.read_bytes()!=raw:raise Denied('Saved citation version changed')
                else:write_durable(version,raw)
                canonical=os.path.relpath(c._path(db,row),c._path(db,target).parent).replace('\\','/')
                fixed=os.path.relpath(version,c._path(db,target).parent).replace('\\','/')
                label=f'{key} §L{start}–{end}'
                links.append(f'[{label}](<{quote(canonical,safe="/.-_")}>) [v{revision}](<{quote(fixed,safe="/.-_")}>)')
                adopted.append({'occurrence':commit_id+'-'+str(n+1),'id':key,'revision':revision,
                                'start_line':start,'end_line':end,'quote':excerpt,'digest':digest(raw)})
            manifest={'schema':'anc-citations-v1','task':ctx.task,'event':event,'anchor':anchor,
                      'commit':commit_id,'adopted':adopted}
            payload='\n\n'+text.rstrip()+'\n'
            if links:payload+='引用：'+'；'.join(links)+'。\n'
            payload+='^'+anchor+'\n<!-- anc-citations:'+json.dumps(manifest,ensure_ascii=True,sort_keys=True)+' -->\n'
            # This protected intent is a projection of the exact CL passage,
            # not editable business knowledge. Join to the committed digest.
            prior=db.execute('SELECT body,digest FROM citation_batches WHERE id=?',(commit_id,)).fetchone()
            stored=json.dumps(manifest,ensure_ascii=False,sort_keys=True)
            if prior and prior[0]!=stored:raise Denied('Citation occurrence changed on retry')
            before=c.version_bytes(db,target,base) if base else b''
            expected=digest(before+payload.encode())
            if prior and prior['digest']!=expected:
                # A version conflict has no commit yet. The caller can rebase
                # its same passage after reading the current CL. Keep the old
                # preparation in audit, and bind this intent to the new bytes.
                if db.execute('SELECT 1 FROM commits WHERE id=?',(commit_id,)).fetchone():
                    raise Denied('Committed or pending citation cannot change on retry')
                if base!=target['revision']:
                    raise Denied('Read the current CL version before rebasing this passage')
                db.execute('UPDATE citation_batches SET digest=? WHERE id=?',(expected,commit_id))
            db.execute('INSERT OR IGNORE INTO citation_batches VALUES(?,?,?,?,?)',
                       (commit_id,ctx.task,event,stored,expected))
            if not prior or prior['digest']!=expected:
                c.audit(db,ctx.principal,ctx.task,'citations.prepared',
                        {'manifest':manifest,'digest':expected})
        receipt=c.append_event(ctx,event,base,payload,commit_id,take_over)
        return {k:receipt[k] for k in ('id','document','revision','state')} | {'position':event+'#^'+anchor,'citations':len(adopted)}


def _collect(c,db,policy,ctx,rows,source,event,revision,excerpt):
    result=[]
    for row in rows:
        item=json.loads(row['body'])
        if event and item['event']!=event:continue
        _,resource=c._document(db,item['event'])
        if not policy.can_read(ctx,resource):continue
        for use in item['adopted']:
            if source and use['id']!=source:continue
            if revision is not None and use['revision']!=revision:continue
            if excerpt is not None and use['quote']!=excerpt:continue
            _,resource=c._document(db,use['id'])
            if not policy.can_read(ctx,resource):continue
            result.append(dict(use,event=item['event'],event_revision=row['revision'],
                               position=item['event']+'#^'+item['anchor'],task=item['task'],time=row['created']))
    return result


_BATCH="SELECT b.body,c.revision,c.created FROM citation_batches b JOIN commits c ON c.id=b.id AND c.document=b.event AND c.task=b.task AND c.digest=b.digest WHERE c.state='committed'"


def occurrences(c,ctx,source=None,event=None,revision=None,excerpt=None):
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx)
        for key in (source,event):
            if key:policy.require_read(ctx,c._document(db,key)[1])
        # Index-free prefilter only narrows the scan; each use is still matched exactly.
        mark='"id": '+json.dumps(source,ensure_ascii=False) if source else ''
        rows=db.execute(_BATCH+" AND (?='' OR instr(b.body,?)>0) ORDER BY c.created",(mark,mark)).fetchall()
        result=_collect(c,db,policy,ctx,rows,source,event,revision,excerpt)
        c.audit(db,ctx.principal,ctx.task,'citations.located',{'source':source,'event':event,'count':len(result)})
    return result


IMPACT_PAGE=80
IMPACT_ROWS=20000  # cheap indexed audit rows walked per call (~12 ms per 2000 on 200k synthetic rows)
IMPACT_READS=200   # recording bodies (file reads) opened per call


def impact(c,ctx,source,revision=None,after=0,limit=IMPACT_PAGE):
    """Deliveries that may rest on a recorded adoption of `source`, resumable.

    Joins protected adoptions to the reviewed recording of the same task, its saved
    reply and the actual outbox state. Only registered, directly adopted uses are
    found; unregistered history and mere task/time proximity are not inferred, and
    the result never says who should be told. Everything is filtered by what the
    current audience may read, so an unreadable reply or source is simply absent.

    Cost per call is bounded and independent of total history: an indexed walk of
    at most IMPACT_ROWS `records.reviewed` rows after the cursor; per task, its own
    citation batches through the task index (no whole-table scan, no global
    occurrence list); at most IMPACT_READS recording bodies opened. `next_after`
    resumes exactly after the last examined row, so pages neither skip nor repeat.
    A recording yields one entry per adopted revision of the source.
    """
    if type(limit) is not int or not 1<=limit<=100 or type(after) is not int or after<0:
        raise ValueError('Bounded impact page required')
    mark='"id": '+json.dumps(source,ensure_ascii=False)
    found=[];walked=0;reads=0;last=after;more=False;uses_by_task={}
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx)
        src_row,src_resource=c._document(db,source);policy.require_read(ctx,src_resource)
        current=src_row['revision']
        rows=db.execute("SELECT id,task,detail FROM audit WHERE action='records.reviewed' AND id>? ORDER BY id LIMIT ?",
                        (after,IMPACT_ROWS+1)).fetchall()
        for row in rows:
            if walked>=IMPACT_ROWS or reads>=IMPACT_READS or len(found)>=limit:
                more=True;break
            walked+=1;last=row['id']
            task=row['task']
            if task not in uses_by_task:
                batches=[b for b in db.execute(_BATCH+" AND b.task=?",(task,)) if mark in b['body']]
                uses_by_task[task]={u['occurrence']:u for u in _collect(c,db,policy,ctx,batches,source,None,revision,None)}
            by_occurrence=uses_by_task[task]
            if not by_occurrence:continue
            reads+=1
            detail=json.loads(row['detail'])
            try:
                record,resource=c._document(db,detail['record'])
                if not policy.can_read(ctx,resource):continue
                body=json.loads(c._read(db,record))
                reply_row,reply_resource=c._document(db,body['reply']['document'])
                if not policy.can_read(ctx,reply_resource):continue
            except Exception:
                continue
            deliveries=None
            for item in body.get('adopted',[]):
                if item['id']!=source:continue
                matched=[o for o in item.get('occurrences',[]) if o in by_occurrence]
                if not matched:continue
                if deliveries is None:
                    deliveries=[{'outbox':d['id'],'state':d['state'],'created':d['created']} for d in db.execute(
                        'SELECT id,state,created FROM outbox WHERE payload_document=? ORDER BY created',(body['reply']['document'],))]
                used=by_occurrence[matched[0]]['revision']
                found.append({'reply':body['reply']['document'],'reply_revision':body['reply']['revision'],
                              'recording':detail['record'],'task':task,
                              'adopted_revision':used,'source_current_revision':current,
                              'superseded':used<current,'occurrences':matched,'deliveries':deliveries})
        c.audit(db,ctx.principal,ctx.task,'citations.impact_located',
                {'source':source,'revision':revision,'after':after,'returned':len(found),'walked':walked,'opened':reads})
    return {'source':source,'may_be_affected':found,'walked':walked,'opened':reads,
            'next_after':last if more else None,
            'coverage':'仅已登记的直接采用及其已保存回复、实际发送状态；按当前全体接收人可读过滤；分页续查用next_after，直到其为空才算查完。不含旧版未登记采用，也不代表这些交付确已错误或应通知谁，须模型核对并遵守现有授权。'}


def counts(uses):
    grouped={}
    for use in uses:
        key=(use['id'],use['revision'],use['quote'])
        row=grouped.setdefault(key,{'id':use['id'],'revision':use['revision'],'quote':use['quote'],'total':0})
        row['total']+=1
    return list(grouped.values())


def rebuild(c):
    """Maintenance-only projection recovery from protected action originals.

    Never infer adoptions from arbitrary Markdown links. Both the registered
    passage call and its exact committed CL version must remain verifiable.
    """
    restored=0
    with c.lock,c.db.transaction() as db:
        preparations={}
        for audit in db.execute("SELECT task,detail FROM audit WHERE action='citations.prepared' ORDER BY id").fetchall():
            preparations[json.loads(audit['detail'])['manifest']['commit']]=audit
        for audit in preparations.values():
            proof=json.loads(audit['detail']);item=proof['manifest']
            commit=db.execute("SELECT * FROM commits WHERE id=? AND state='committed'",(item['commit'],)).fetchone()
            if not commit:continue
            if (commit['task'],commit['document'],commit['digest'])!=(audit['task'],item['event'],proof['digest']):
                raise Denied('Citation recovery receipt differs from committed CL')
            row,_=c._document(db,item['event'])
            raw=c.version_bytes(db,row,commit['revision'])
            marker='<!-- anc-citations:'+json.dumps(item,ensure_ascii=True,sort_keys=True)+' -->'
            if marker not in raw.decode():raise Denied('Citation recovery passage missing')
            body=json.dumps(item,ensure_ascii=False,sort_keys=True)
            old=db.execute('SELECT body,digest FROM citation_batches WHERE id=?',(item['commit'],)).fetchone()
            if old and (old['body'],old['digest'])!=(body,proof['digest']):raise Denied('Citation index differs from original receipt')
            if not old:
                db.execute('INSERT INTO citation_batches VALUES(?,?,?,?,?)',
                           (item['commit'],item['task'],item['event'],body,proof['digest']))
                restored+=1
    return {'restored':restored,'basis':'protected passage action and exact committed CL; no inferred historic uses'}


def catalog(c,ctx,limit=80,offset=0,snapshot=None):
    from .retrieval import source_rows,page_check,title
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('Bounded catalog required')
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx);result=[]
        rows=source_rows(c,db,policy,ctx,['event','context'])
        snap=page_check(rows,ctx,{'catalog':True},offset,snapshot)
        for row in rows[offset:offset+limit]:
            text=c._read(db,row).decode('utf-8')
            changed=db.execute("SELECT MAX(created) FROM commits WHERE document=? AND state='committed'",(row['id'],)).fetchone()[0]
            result.append({'id':row['id'],'kind':row['kind'],'revision':row['revision'],
                           'title':title(text,row['id']),'updated':changed})
            db.execute('INSERT OR IGNORE INTO task_reads VALUES(?,?)',(ctx.task,row['id']))
        more=offset+len(result)<len(rows)
        c.audit(db,ctx.principal,ctx.task,'cases.located',{'returned':[r['id'] for r in result],'truncated':more,'offset':offset,'snapshot':snap})
        return {'entries':result,'truncated':more,'next_offset':offset+len(result) if more else None,'snapshot':snap,
                'coverage':'当前全体接收人可读事项及Context入口；不是正文已读或语义匹配证明。'}
