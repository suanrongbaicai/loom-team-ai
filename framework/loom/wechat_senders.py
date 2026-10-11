"""Exact shard-local sender metadata, retaining corrections beside originals."""
import hashlib
import json
import re
import sqlite3
from pathlib import Path

from .control import digest, within
from .policy import Denied, utcnow


def sender(conn, sender_id, content, chat):
    if type(sender_id) is not int and not (isinstance(sender_id,str) and sender_id.isdigit()):
        return None
    found=conn.execute('SELECT user_name FROM Name2Id WHERE rowid=?',(int(sender_id),)).fetchall()
    if len(found)!=1 or not isinstance(found[0][0],str) or not found[0][0]:return None
    username=found[0][0]
    prefix=re.match(r'^([A-Za-z0-9_@.\-]{3,128}):\r?\n',content)
    if chat.endswith('@chatroom') and prefix and prefix[1]!=username:return None
    return {'username':username,'sender_id':int(sender_id),'source':'same_shard.Name2Id',
            'content_prefix_checked':bool(chat.endswith('@chatroom') and prefix)}


def cached_metadata(body, cache):
    """Read identities only for exact rows already present in this capture."""
    connections={};result=[];contact=None
    try:
        cp=within(cache,'contact__contact.db')
        if cp.exists():contact=sqlite3.connect(cp.as_uri()+'?mode=ro',uri=True)
        for index,message in enumerate(body['messages']):
            shard=message['source_shard'].replace('\\','/')
            if not re.fullmatch(r'message/message_\d+\.db',shard):raise Denied('Unrecognized captured shard')
            table='Msg_'+hashlib.md5(message['chat'].encode()).hexdigest()
            if message['source_table']!=table:raise Denied('Captured chat/table mismatch')
            path=within(cache,shard.replace('/','__'))
            if not path.exists():continue
            if shard not in connections:
                connections[shard]=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
                connections[shard].row_factory=sqlite3.Row
            conn=connections[shard]
            rows=conn.execute('SELECT real_sender_id,message_content FROM "'+table+'" WHERE local_id=? AND server_id=? AND create_time=?',
                             (message['local_id'],message['server_id'],message['create_time'])).fetchall()
            if len(rows)!=1:continue
            row=rows[0];raw=row['message_content']
            if message.get('raw_content_hex') and (not isinstance(raw,bytes) or raw.hex()!=message['raw_content_hex']):continue
            found=sender(conn,row['real_sender_id'],message.get('content',''),message['chat'])
            if not found:continue
            names=contact.execute('SELECT nick_name,remark FROM contact WHERE username=?',(found['username'],)).fetchall() if contact else []
            # Display labels are local contact metadata, never proof of a real person.
            found.update(index=index,shard=shard,table=table,local_id=message['local_id'],server_id=message['server_id'],
                create_time=message['create_time'],name=(names[0][1] or names[0][0] or found['username']) if len(names)==1 else found['username'])
            result.append(found)
        return result
    finally:
        for conn in connections.values():conn.close()
        if contact:contact.close()


def view(c,ctx,source,saved,body,collector):
    config=getattr(collector,'config',None)
    if not config:return None
    accounts=[key for key,value in config.get('accounts',{}).items()
              if value.get('enabled') and value.get('wxid')==body.get('account')]
    if len(accounts)!=1:return None
    key='wechat-senders-'+digest((ctx.scope+':'+source+':'+digest(saved['text'].encode())).encode())
    with c.lock:
        with c.db.transaction() as db:
            policy=c._check(db,ctx,True);policy.require_read(ctx,c._document(db,source)[1])
            if not db.execute('SELECT 1 FROM wechat_imports WHERE document=?',(source,)).fetchone():raise Denied('Verified capture required')
            current=c._document(db,source)[0]
            if current['revision']!=saved['revision'] or current['digest']!=digest(saved['text'].encode()):raise Denied('Captured original changed')
            existing=db.execute('SELECT * FROM documents WHERE id=?',(key,)).fetchone()
        if existing and existing['revision']:
            value=json.loads(c.read(ctx,key)['text'])
        else:
            cache=within(c.root,'WeChatRuntime/cache/'+accounts[0])
            if not cache.exists():return None
            try:rows=cached_metadata(body,cache)
            except (sqlite3.Error,OSError) as error:
                with c.db.transaction() as db:
                    c.audit(db,ctx.principal,ctx.task,'wechat.senders.unavailable',{'source':source,'error_type':type(error).__name__})
                return None  # Existing messages remain readable; no identity guess.
            if not rows:return None
            value={'schema':'loom-wechat-sender-evidence-v1','source':source,'source_revision':saved['revision'],
                   'source_digest':digest(saved['text'].encode()),'observed_at':utcnow(),'messages':rows,
                   'coverage':'已保存原消息的同分库编号核对；昵称是本地联系人称呼，不证明现实身份；原历史不覆盖。'}
            c.register(ctx,key,'data')
            c.commit(ctx,key,0,json.dumps(value,ensure_ascii=False,sort_keys=True),key,c.lease(ctx,key))
            c.read(ctx,key)
        with c.db.transaction() as db:
            c._check(db,ctx);policy=c.policy(db);policy.require_read(ctx,c._document(db,source)[1])
            c.audit(db,ctx.principal,ctx.task,'wechat.senders.read',{'source':source,'evidence':key,'resolved_messages':len(value['messages'])})
        return key,{x['index']:x for x in value['messages']}
