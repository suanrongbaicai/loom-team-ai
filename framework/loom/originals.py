"""Retain acquired originals in controlled Data, independently of decoding."""
import json
from pathlib import Path

from .control import digest, identifier, within, write_durable
from .policy import Denied, utcnow

MAX_BYTES=32*1024*1024


def retain(c,ctx,source,part,raw,name,mime='application/octet-stream',coverage='original'):
    identifier(source);identifier(part)
    if not isinstance(raw,bytes) or len(raw)>MAX_BYTES:raise ValueError('Original exceeds 32 MiB storage budget')
    if not isinstance(name,str) or len(name)>240:raise ValueError('Invalid material name')
    sha=digest(raw)
    key='original-'+digest((ctx.scope+':'+source+':'+part+':'+sha).encode())
    with c.lock:
        with c.db.transaction() as db:
            policy=c._check(db,ctx,True);policy.require_read(ctx,c._document(db,source)[1])
            path=within(policy.state['scopes'][ctx.scope]['root'],'Data/originals/'+sha+'.bin')
            if path.exists():
                if path.read_bytes()!=raw:raise Denied('Retained original changed')
            else:write_durable(path,raw)
            old=db.execute('SELECT * FROM documents WHERE id=?',(key,)).fetchone()
        if not old or not old['revision']:
            c.register(ctx,key,'data')
            with c.db.transaction() as db:
                acquired=c._document(db,key)[0]['created']
            body={'schema':'loom-retained-original-v1','source':source,'part':part,'name':name,'mime':mime,
                  'sha256':sha,'bytes':len(raw),'path':'Data/originals/'+sha+'.bin','coverage':coverage,
                  'acquired_at':acquired,'understood':'not established by storage'}
            c.commit(ctx,key,0,json.dumps(body,ensure_ascii=False,sort_keys=True),key,c.lease(ctx,key,take_over=True))
        with c.db.transaction() as db:
            policy=c._check(db,ctx,True);policy.require_read(ctx,c._document(db,source)[1])
            db.execute('INSERT OR IGNORE INTO materials VALUES(?,?,?)',(key,source,part))
            c.audit(db,ctx.principal,ctx.task,'original.retained',{'document':key,'source':source,'part':part,'bytes':len(raw),'sha256':sha,'coverage':coverage})
    return key


def get(c,ctx,key,*,with_reference=False):
    saved=c.read(ctx,key)
    body=json.loads(saved['text'])
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx)
        row=db.execute('SELECT * FROM materials WHERE document=?',(key,)).fetchone()
        if not row or body.get('schema')!='loom-retained-original-v1':raise Denied('Retained original unavailable')
        policy.require_read(ctx,c._document(db,row['source'])[1])
        # Retained bytes live in this document's controlled scope, never a path
        # supplied by the model or a path in the external source message.
        doc,_=c._document(db,key)
        path=within(policy.state['scopes'][doc['scope']]['root'],'Data/originals/'+body['sha256']+'.bin')
        with path.open('rb') as f:raw=f.read(MAX_BYTES+1)
        if len(raw)!=body['bytes'] or digest(raw)!=body['sha256']:raise Denied('Original bytes unavailable or changed')
        db.execute('INSERT OR IGNORE INTO task_reads VALUES(?,?)',(ctx.task,row['source']))
        c.audit(db,ctx.principal,ctx.task,'original.read',{'document':key,'source':row['source'],'sha256':body['sha256'],'bytes':len(raw)})
    if with_reference:
        # Reference the exact retained file manifest, not a fabricated quote
        # from binary bytes. The caller still chooses whether it was adopted.
        reference={'id':key,'revision':saved['revision'],
                   'quote':'"sha256": "'+body['sha256']+'"'}
        return body,raw,reference
    return body,raw


def find(c,ctx,source,part=None):
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx);policy.require_read(ctx,c._document(db,source)[1])
        rows=db.execute('SELECT * FROM materials WHERE source=? ORDER BY document',(source,)).fetchall()
        return [r['document'] for r in rows if (part is None or r['part']==part)
                and policy.can_read(ctx,c._document(db,r['document'])[1])]


def adopt_cached(c,ctx,source,key):
    """Reuse exactly the authorized source's bytes in the receiving scope.

    A private cache descriptor is not itself a grant. The current audience
    must be permitted to read the original message including its attachment.
    No unrelated private record is copied or exposed.
    """
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx,True);policy.require_read(ctx,c._document(db,source)[1])
        stored=db.execute('SELECT source FROM materials WHERE document=?',(key,)).fetchone()
        if not stored or stored['source']!=source:raise Denied('Cached original source differs')
        row,_=c._document(db,key);body=json.loads(c._read(db,row))
        path=within(policy.state['scopes'][row['scope']]['root'],'Data/originals/'+body['sha256']+'.bin')
        with path.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
        if len(raw)!=body['bytes'] or digest(raw)!=body['sha256']:raise Denied('Cached original changed')
    return retain(c,ctx,source,body['part'],raw,body['name'],body['mime'],body['coverage'])
