"""Retain official full-file exports, with source-message and actual audience checks."""
import io
import json
import re
import urllib.request
import zipfile

from .control import digest,identifier
from .policy import Denied,utcnow
from .originals import retain,MAX_BYTES
from .report_leaf import encoded
from .feishu_reports import CloudFailure


def download(client,token):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',token):raise Denied('Unexpected export token')
    req=urllib.request.Request(client.ORIGIN+'/open-apis/drive/v1/export_tasks/file/'+token+'/download',headers={'Authorization':'Bearer '+client.token()})
    with client.http.open(req,timeout=20) as response:
        raw=response.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES:raise ValueError('导出文件超过32 MiB，需按明确范围拆分')
    return raw


class CloudCopy:
    def __init__(self,cloud,download_file=download):
        self.cloud,self.c,self.download=cloud,cloud.c,download_file

    def run(self,ctx,request_id,url,source,guard):
        identifier(request_id);identifier(source)
        match=re.fullmatch(r'https://(?:[A-Za-z0-9-]+\.)?feishu\.cn/(docx|sheets)/([A-Za-z0-9]{10,27})(?:\?[^#\s]*)?(?:#[^\s]*)?',url)
        if not match:raise ValueError('请提供飞书新版文档或电子表格的直接链接')
        kind='sheet' if match[1]=='sheets' else 'docx';token=match[2];extension='xlsx' if kind=='sheet' else 'docx'
        app,people=self.cloud.binding(ctx);client=self.cloud.clients[app]
        # Publication ids and their report ids name the same protected source.
        # Resolve only a verified publication in this exact audience.
        with self.c.db.transaction() as db:
            publication=db.execute("SELECT report,body FROM cloud_publications WHERE id=? AND scope=? AND state='published'",(source,ctx.scope)).fetchone()
        if publication:
            stored=json.loads(publication['body'])
            if stored.get('token')!=token or stored.get('app')!=app or stored.get('people')!=people:raise Denied('发布凭据与源链接或接收人不符')
            source=publication['report']
        ident='cloud-copy-'+digest((ctx.scope+':'+request_id).encode())
        expected={'url':url,'source':source,'app':app,'people':people,'format':extension}
        def check():
            guard()
            if self.cloud.binding(ctx)!=(app,people):raise Denied('复制接收范围已改变')
            saved=self.c.read(ctx,source)
            with self.c.db.transaction() as db:
                message=db.execute('SELECT * FROM messages WHERE resource=?',(source,)).fetchone()
                publications=db.execute("SELECT body FROM cloud_publications WHERE scope=? AND report=? AND state='published'",(ctx.scope,source)).fetchall()
            published=any((b:=json.loads(r[0])).get('token')==token and b.get('app')==app and b.get('people')==people for r in publications)
            if not published:
                if not message:raise Denied('需要本人实际消息中的文档链接，或当前会话已发布的报表来源')
                original=json.loads(saved['text'])
                if original.get('sender')!=people[ctx.principal] or url not in str(original.get('content','')):raise Denied('链接未出现在当前本人发来的原始消息中')
            members=self.cloud.fetch(client,'GET',f'/open-apis/drive/v1/permissions/{token}/members?type={kind}')
            allowed={m.get('member_id') for m in members.get('items',[]) if m.get('member_type')=='openid' and m.get('perm') in ('view','edit','full_access')}
            if not set(people.values()).issubset(allowed):raise Denied('尚未核实所有接收人对该文件的权限；需核清实际成员或群/部门继承权限，不能只凭Bot可读导入')
            with self.c.db.transaction() as db:self.c.audit(db,ctx.principal,ctx.task,'report.copy_permissions',{'copy':ident,'source':source,'file':token,'members':members})
        check()
        with self.c.db.transaction() as db:old=db.execute('SELECT body FROM cloud_copies WHERE id=? AND scope=?',(ident,ctx.scope)).fetchone()
        body=json.loads(old[0]) if old else dict(expected,state='new',created=utcnow())
        if any(body.get(k)!=v for k,v in expected.items()):raise Denied('同一复制编号不能更换源文件或接收人')
        def save():
            with self.c.db.transaction() as db:
                self.c._check(db,ctx,True)
                db.execute('INSERT OR REPLACE INTO cloud_copies VALUES(?,?,?)',(ident,ctx.scope,encoded(body).decode()))
                self.c.audit(db,ctx.principal,ctx.task,'report.cloud_copy',{'copy':ident,**body})
        def result():return {k:body[k] for k in ('state','original','receipt','created','completed_at','reason') if k in body}|{'copy':ident,'data':ident}
        if body['state'] in ('saved','unknown','failed'):return result()
        self.c.register(ctx,ident,'data')
        if not self.c.read(ctx,ident)['revision']:
            self.c.commit(ctx,ident,0,encoded({'schema':'loom-cloud-copy-v1',**expected,'requested_at':body['created']}).decode(),ident,self.c.lease(ctx,ident,take_over=True))
        if not body.get('ticket'):
            # Credential acquisition fails before any export submission; do not mark that as unknown.
            if hasattr(client,'token'):client.token()
            with self.c.db.transaction() as db:
                self.c._check(db,ctx,True)
                claimed=db.execute('SELECT body FROM cloud_copies WHERE id=?',(ident,)).fetchone()
                if claimed and json.loads(claimed[0]).get('state')!='new':
                    body=json.loads(claimed[0]);return result()
                body['state']='unknown'
                db.execute('INSERT OR REPLACE INTO cloud_copies VALUES(?,?,?)',(ident,ctx.scope,encoded(body).decode()))
                self.c.audit(db,ctx.principal,ctx.task,'report.export_request',{'copy':ident,**body})
            try:info=self.cloud.fetch(client,'POST','/open-apis/drive/v1/export_tasks',{'type':kind,'token':token,'file_extension':extension})
            except CloudFailure as error:
                if not error.unknown:body['state']='new';save()
                raise
            body['ticket']=info['ticket'];body['state']='exporting';body['create_response']=info;save()
        ticket=body['ticket']
        if not re.fullmatch(r'[0-9]{1,40}',ticket):raise Denied('Unexpected export ticket')
        check()
        info=self.cloud.fetch(client,'GET','/open-apis/drive/v1/export_tasks/'+ticket+'?token='+token)['result']
        body['export_result']=info;save()
        status=info.get('job_status')
        if status in (1,2):return result()|{'next':'飞书正在导出，稍后使用同一id继续，不重新提交'}
        if status!=0:
            body.update(state='failed',reason='飞书导出未完成：'+str(info.get('job_error_msg',status)));save();return result()
        size=info.get('file_size')
        if type(size) is not int or not 0<size<=MAX_BYTES:raise ValueError('导出大小不可用或超过32 MiB')
        raw=self.download(client,info['file_token']);check()
        if len(raw)!=size:raise Denied('导出文件下载不完整')
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if '[Content_Types].xml' not in z.namelist() or ('word/document.xml' if extension=='docx' else 'xl/workbook.xml') not in z.namelist():raise Denied('导出文件格式不符')
            if sum(f.file_size for f in z.infolist())>128*1024*1024:raise ValueError('展开的导出文件过大')
            if z.testzip():raise Denied('导出文件校验失败')
        body['original']=retain(self.c,ctx,ident,'official-export',raw,'cloud-export.'+extension,'application/vnd.openxmlformats-officedocument.'+('wordprocessingml.document' if extension=='docx' else 'spreadsheetml.sheet'),'飞书完整文件导出；不包含平台历史版本、评论或未由平台导出的嵌入内容；实际理解另核对')
        body['completed_at']=utcnow()
        body['receipt']=retain(self.c,ctx,ident,'export-receipt',encoded(body),'export-receipt.json','application/json','actual export request/result and acquired file identity')
        body['state']='saved';save();return result()
