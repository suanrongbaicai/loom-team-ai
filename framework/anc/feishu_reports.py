"""Fixed native cloud operations. Credentials and recipients stay in the controller."""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .control import digest,identifier
from .policy import Denied,utcnow
from .report_leaf import encoded,sheet_values,sheet_diff
from .report_templates import metrics
from .originals import retain,get


class CloudFailure(RuntimeError):
    def __init__(self,code=None,unknown=False):
        self.unknown=unknown
        super().__init__('飞书操作结果未确认，请先核实已有文件，不可重新创建。' if unknown else '飞书接口未完成，错误码：'+str(code))


def request(client,method,path,body=None,upload=None):
    if not path.startswith('/open-apis/') or '\\' in path:raise ValueError('Fixed API path required')
    headers={'Authorization':'Bearer '+client.token(),'Content-Type':'application/json; charset=utf-8'}
    raw=encoded(body) if body is not None else None
    if upload:
        boundary='anc'+uuid.uuid4().hex;fields,data=upload;parts=[]
        for k,v in fields.items():
            parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="'+k+'"\r\n\r\n'+str(v)+'\r\n').encode())
        parts.append(('--'+boundary+'\r\nContent-Disposition: form-data; name="file"; filename="chart.png"\r\nContent-Type: image/png\r\n\r\n').encode()+data+b'\r\n')
        raw=b''.join(parts)+('--'+boundary+'--\r\n').encode();headers['Content-Type']='multipart/form-data; boundary='+boundary
    req=urllib.request.Request(client.ORIGIN+path,data=raw,headers=headers,method=method)
    try:
        with client.http.open(req,timeout=20) as response:raw=response.read(12*1024*1024+1)
        if len(raw)>12*1024*1024:raise ValueError('Cloud response limit')
        value=json.loads(raw)
    except urllib.error.HTTPError as exc:
        try:code=json.loads(exc.read(65536)).get('code',exc.code)
        except Exception:code=exc.code
        raise CloudFailure(code,unknown=exc.code>=500 and method!='GET') from None
    except Exception:raise CloudFailure(unknown=method!='GET') from None
    if value.get('code')!=0:raise CloudFailure(value.get('code'))
    return value.get('data',{})


def block(text,heading=False):
    key='heading2' if heading else 'text'
    return {'block_type':4 if heading else 2,key:{'elements':[{'text_run':{'content':text}}]}}


def native_blocks(spec):
    """Phone-readable paragraphs, keeping exact values and notes outside the grid."""
    result=[]
    if spec.get('subtitle'):result.append(block(spec['subtitle']))
    if spec.get('summary'):result.append(block(spec['summary']))
    if spec['template'] not in ('table','brief','kanban','gantt'):result.append({'block_type':27,'image':{}})
    for key,total in metrics(spec)['totals'].items():
        col=next(c for c in spec['columns'] if c['key']==key)
        result.append(block(col['label']+'：'+(total['sum']+(' '+col['unit'] if col.get('unit') else '') if total['known'] else '未提供')+'（已知 '+str(total['known'])+' 项'+('，另有 '+str(total['missing'])+' 项未提供' if total['missing'] else '')+'）'))
    rows=spec['rows']
    if spec['template']=='kanban':
        status=spec['view']['status'];stages=list(dict.fromkeys(r['values'][status] for r in rows))
        rows=[r for stage in stages for r in rows if r['values'][status]==stage]
    current_stage=object()
    sources={}
    for row in rows:
        if spec['template']=='kanban' and row['values'][status]!=current_stage:
            current_stage=row['values'][status];result.append(block('阶段：'+str(current_stage or '未提供'),True))
        result.append(block(str(row['values'][spec['columns'][0]['key']]) if row['values'][spec['columns'][0]['key']] is not None else '未提供',True))
        text='\n'.join(c['label']+'：'+('未提供' if row['values'][c['key']] is None else str(row['values'][c['key']]))+(' '+c['unit'] if c.get('unit') else '') for c in spec['columns'][1:])
        if text:result.append(block(text))
        if row.get('note'):result.append(block(row['note']))
        for ref in row['evidence']:
            key=(ref['id'],ref['revision'],ref['quote'])
            sources.setdefault(key,[]).append(str(row['values'][spec['columns'][0]['key']]))
    result.append(block('依据摘录',True))
    for (_,_,quote),labels in sources.items():
        # Full references remain in Data. The reading copy uses brief, deduplicated excerpts.
        labels=list(dict.fromkeys(labels));caption='、'.join(labels[:4])+('等 '+str(len(labels))+' 项' if len(labels)>4 else '')
        excerpt=quote if len(quote)<=400 else quote[:400]+'…（节选，完整原件已入库）'
        result.append(block(caption+'：\n'+excerpt))
    return result


class CloudReports:
    def __init__(self,control,leaf,clients,fetch=request):
        self.c,self.leaf,self.clients,self.fetch=control,leaf,clients,fetch

    def binding(self,ctx):
        with self.c.db.transaction() as db:
            p=self.c._check(db,ctx);scope=p.state['scopes'][ctx.scope];transport=scope.get('transport',{})
            apps=[r[0] for r in db.execute('SELECT DISTINCT app FROM processing WHERE task=?',(ctx.task,))]
            if not apps:apps=transport.get('apps',[])
            if len(apps)!=1 or apps[0] not in self.clients:raise Denied('当前会话没有唯一的飞书应用绑定')
            app=apps[0];people={}
            for who in sorted(p.recipients(ctx)):
                bindings=[b for b in p.state['members'][who].get('bindings',[]) if b.get('platform')=='feishu' and b.get('app')==app and b.get('tenant')==transport.get('tenant') and b.get('evidence')]
                if len(bindings)!=1:raise Denied('接收人的飞书身份绑定不完整')
                people[who]=bindings[0]['account']
            return app,people

    def _job(self,ctx,jobid):
        identifier(jobid)
        with self.c.db.transaction() as db:
            self.c._check(db,ctx);row=db.execute('SELECT * FROM cloud_publications WHERE id=?',(jobid,)).fetchone()
            if not row or row['scope']!=ctx.scope:raise Denied('Cloud publication unavailable')
            report=row['report'];body=json.loads(row['body']);state=row['state']
        self.leaf.load(ctx,report)
        return {'publication':jobid,'report':report,'state':state,**body}

    def status(self,ctx,jobid):
        job=self._job(ctx,jobid)
        return {k:job[k] for k in ('publication','report','state','stage','format','url','receipt','error','completed_at') if k in job}

    def publish(self,ctx,request_id,report,format,guard):
        identifier(request_id)
        if format not in ('docx','sheet'):raise ValueError('Use native docx or sheet')
        saved=self.leaf.load(ctx,report);files=self.leaf.files(ctx,report);spec=saved['spec']
        if format=='docx' and len(spec['rows'])>100:raise ValueError('Phone report supports 100 rows; use a sheet for more')
        app,people=self.binding(ctx);client=self.clients[app]
        jobid='cloud-'+digest((ctx.scope+':'+request_id).encode())
        body={'app':app,'people':people,'format':format,'stage':'saved','report':report,'steps':[]}
        planned={'format':format,'spec':spec,'native':native_blocks(spec) if format=='docx' else sheet_values(spec),'recipients':people}
        with self.c.lock,self.c.db.transaction() as db:
            self.c._check(db,ctx,True)
            previous=db.execute('SELECT * FROM cloud_publications WHERE id=?',(jobid,)).fetchone()
            if previous:
                if previous['report']!=report or previous['format']!=format:raise Denied('Publication id already used for different content')
                body=json.loads(previous['body'])
                if body['app']!=app or body['people']!=people:raise Denied('Publication audience changed')
                if previous['state'] in ('published','unknown') or body.get('pending'):
                    reused={'reused':True,'created':previous['created'],'meaning':'这不是新做的：同一份报表、同一接收范围的飞书副本已在%s（UTC）发布过，返回的是原来的链接。答复里必须明说“这是之前做过的那份（做于…）”，不要说成刚做好；如果需要新版，要改数据或要求重新生成。' % previous['created']} if previous['state']=='published' and not body.get('pending') else {}
                    return {'publication':jobid,'state':previous['state'] if not body.get('pending') else 'unknown',**{k:body[k] for k in ('url','stage','error','receipt') if k in body},**reused}
        if previous:
            intent=body['intent'];planned=json.loads(get(self.c,ctx,intent)[1])
        else:
            intent=retain(self.c,ctx,report,'cloud-plan-'+digest(request_id.encode())[:16],encoded(planned),'cloud-intent.json','application/json','complete intended cloud content and recipients')
            body['intent']=intent
            with self.c.db.transaction() as db:
                self.c._check(db,ctx,True)
                db.execute('INSERT INTO cloud_publications VALUES(?,?,?,?,?,?,?,?)',(jobid,ctx.task,ctx.scope,report,format,'publishing',encoded(body).decode(),utcnow()))
        def save(state):
            with self.c.db.transaction() as db:
                db.execute('UPDATE cloud_publications SET state=?,body=? WHERE id=?',(state,encoded(body).decode(),jobid))
                self.c.audit(db,ctx.principal,ctx.task,'report.cloud_stage',{'publication':jobid,'state':state,'stage':body['stage'],'file':body.get('token')})
        def check():
            guard();self.leaf.load(ctx,report)
            if self.binding(ctx)!=(app,people):raise Denied('Cloud recipients changed')
        def call(stage,method,path,payload=None,upload=None):
            check();body['stage']=stage
            fingerprint=digest(encoded([method,path,payload,{'fields':upload[0],'digest':digest(upload[1])} if upload else None]))
            old=next((s for s in body['steps'] if s['stage']==stage),None)
            if old and method!='GET':
                if old.get('request_digest')!=fingerprint:raise Denied('Saved cloud operation differs; do not repeat')
                return old['response']
            if method!='GET':body['pending']={'stage':stage,'request_digest':fingerprint}
            save('publishing')
            with self.c.db.transaction() as db:
                self.c.audit(db,ctx.principal,ctx.task,'report.cloud_request',{'publication':jobid,'stage':stage,'method':method,'path':path,'request_digest':fingerprint,'intent':intent})
            try:result=self.fetch(client,method,path,payload,upload)
            except Exception as error:
                # A platform rejection or failure before credential acquisition did not perform this write.
                from .membership import AudienceUnavailable
                if (isinstance(error,CloudFailure) and not error.unknown) or isinstance(error,AudienceUnavailable):body.pop('pending',None)
                raise
            with self.c.db.transaction() as db:
                self.c.audit(db,ctx.principal,ctx.task,'report.cloud_response',{'publication':jobid,'stage':stage,'response':result})
            # Save response even if a cancellation arrived while the request was in flight.
            body.pop('pending',None)
            body['steps']=[s for s in body['steps'] if s['stage']!=stage]
            body['steps'].append({'stage':stage,'request_digest':fingerprint,'response':result});save('publishing')
            check();return result
        try:
            check()
            if format=='docx':
                info=call('create','POST','/open-apis/docx/v1/documents',{'title':spec['title']})['document']
                token=info['document_id'];body['url']='https://feishu.cn/docx/'+token
            else:
                info=call('create','POST','/open-apis/sheets/v3/spreadsheets',{'title':spec['title']})['spreadsheet']
                token=info['spreadsheet_token'];body['url']=info['url']
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',token):raise ValueError('Unexpected cloud id')
            body['token']=token;save('publishing')
            permissions='/open-apis/drive/v2/permissions/'+token+'/public?type='+format
            locked={'external_access_entity':'closed','link_share_entity':'closed','manage_collaborator_entity':'collaborator_full_access'}
            call('lock-sharing','PATCH',permissions,locked)
            got=call('verify-sharing','GET',permissions)['permission_public']
            if any(got.get(k)!=v for k,v in locked.items()):raise Denied('云文档的分享限制没有生效，尚未写入正文')
            members='/open-apis/drive/v1/permissions/'+token+'/members?type='+format
            original_members=call('verify-empty-audience','GET',members)
            prior_grants={people[s['stage'][6:]] for s in body['steps'] if s['stage'].startswith('grant-') and s['stage'][6:] in people}
            if not isinstance(original_members.get('items'),list) or any(not ((m.get('member_type')=='appid' and m.get('member_id')==app) or (m.get('member_type')=='openid' and m.get('member_id') in prior_grants)) for m in original_members['items']):raise Denied('文件已有意外协作者或成员核对不完整，停止写入')
            if format=='sheet':
                sheets=call('sheets','GET','/open-apis/sheets/v3/spreadsheets/'+token+'/sheets/query')['sheets']
                if len(sheets)!=1:raise Denied('Unexpected initial sheet count')
                sid=sheets[0]['sheet_id'];values=planned['native'];end=chr(64+len(values[0]));area=f'{sid}!A1:{end}{len(values)}'
                body['range']=area;body['sheet_id']=sid
                call('write-values','PUT','/open-apis/sheets/v2/spreadsheets/'+token+'/values',{'valueRange':{'range':area,'values':values}})
                call('format-header','PUT','/open-apis/sheets/v2/spreadsheets/'+token+'/style',{'appendStyle':{'range':sid+'!A1:'+end+'1','style':{'font':{'bold':True,'fontSize':'12pt/1.5'},'foreColor':'#FFFFFF','backColor':'#066FD1'}}})
                dimensions='/open-apis/sheets/v2/spreadsheets/'+token+'/dimension_range'
                call('hide-internal-id','PUT',dimensions,{'dimension':{'sheetId':sid,'majorDimension':'COLUMNS','startIndex':1,'endIndex':1},'dimensionProperties':{'visible':False}})
                call('readable-width','PUT',dimensions,{'dimension':{'sheetId':sid,'majorDimension':'COLUMNS','startIndex':2,'endIndex':len(values[0])},'dimensionProperties':{'fixedSize':180}})
                call('freeze-header','POST','/open-apis/sheets/v2/spreadsheets/'+token+'/sheets_batch_update?user_id_type=open_id',{'requests':[{'updateSheet':{'properties':{'sheetId':sid,'title':'资料明细','frozenRowCount':1,'frozenColCount':2}}}]})
                actual=call('verify-values','GET','/open-apis/sheets/v2/spreadsheets/'+token+'/values/'+urllib.parse.quote(area,safe='!:')+'?valueRenderOption=UnformattedValue')['valueRange']['values']
                diff=sheet_diff(spec,actual,expected_header=planned['native'][0])
                if diff['changes'] or diff['missing_rows']:raise Denied('飞书回读值与已保存版本不一致，尚未分享')
            else:
                blocks=native_blocks(spec);created=[]
                for start in range(0,len(blocks),40):
                    result=call('write-blocks-'+str(start),'POST',f'/open-apis/docx/v1/documents/{token}/blocks/{token}/children?document_revision_id=-1&client_token='+digest((jobid+':'+str(start)).encode())[:32],{'children':blocks[start:start+40],'index':-1})
                    created.extend(result['children'])
                for b in created:
                    if b['block_type']==27:
                        if 'chart-png' not in files:raise Denied('Native chart image renderer unavailable')
                        raw=files['chart-png'][1];bid=b['block_id']
                        image=call('upload-chart','POST','/open-apis/drive/v1/medias/upload_all',upload=({'file_name':'chart.png','parent_type':'docx_image','parent_node':bid,'size':len(raw)},raw))
                        call('attach-chart','PATCH',f'/open-apis/docx/v1/documents/{token}/blocks/{bid}?document_revision_id=-1',{'replace_image':{'token':image['file_token']}})
                readback=[];cursor=''
                while True:
                    page=call('verify-blocks-'+str(len(readback)),'GET',f'/open-apis/docx/v1/documents/{token}/blocks/{token}/children?page_size=500'+('&page_token='+urllib.parse.quote(cursor,safe='') if cursor else ''))
                    readback.extend(page['items'])
                    if not page.get('has_more'):break
                    if not page.get('page_token') or page['page_token']==cursor:raise Denied('Cloud pagination incomplete')
                    cursor=page['page_token']
                    if len(readback)>1000:raise Denied('Cloud content unexpectedly grew')
                if len(readback)!=len(blocks):raise Denied('云文档段落数量不一致，尚未分享')
                for expected,actual in zip(blocks,readback):
                    key='heading2' if expected['block_type']==4 else 'text'
                    if expected['block_type']==27:
                        if not actual.get('image',{}).get('token'):raise Denied('Chart attachment missing')
                    elif actual.get('block_type')!=expected['block_type'] or ''.join(e.get('text_run',{}).get('content','') for e in actual.get(key,{}).get('elements',[]))!=expected[key]['elements'][0]['text_run']['content']:raise Denied('云文档回读文字不一致，尚未分享')
            for who,account in people.items():
                call('grant-'+who,'POST',members+'&need_notification=false',{'member_type':'openid','member_id':account,'perm':'edit' if format=='sheet' else 'view'})
            actual=call('verify-final-audience','GET',members).get('items')
            if not isinstance(actual,list):raise Denied('Cloud member response incomplete')
            expected={(account,'openid','edit' if format=='sheet' else 'view') for account in people.values()}
            actual_people={(m.get('member_id'),m.get('member_type'),m.get('perm')) for m in actual if not (m.get('member_type')=='appid' and m.get('member_id')==app)}
            if actual_people!=expected:raise Denied('Actual cloud recipients differ')
            body.pop('error',None);body['stage']='verified';body['completed_at']=utcnow();save('published')
            receipt=retain(self.c,ctx,report,'cloud-receipt-'+digest(request_id.encode())[:16],encoded(body),'cloud-receipt.json','application/json','actual platform responses and readback')
            body['receipt']=receipt;save('published')
            return {'publication':jobid,'source':report,'state':'published','url':body['url'],'receipt':receipt,'format':format,'recipients':list(people),'copy':'本次指定成员可独立查看的飞书副本，原件留在Data。'}
        except Exception as error:
            body['error']=str(error)[:240];save('unknown' if body.get('pending') or (isinstance(error,CloudFailure) and error.unknown) else 'blocked')
            raise

    def import_sheet(self,ctx,publication,guard):
        job=self._job(ctx,publication)
        if job['state']!='published' or job['format']!='sheet':raise Denied('已验证的电子表格副本才能回库')
        if self.binding(ctx)!=(job['app'],job['people']):raise Denied('Import audience differs')
        saved=self.leaf.load(ctx,job['report']);guard()
        area=job['sheet_id']+'!A1:'+chr(65+len(saved['spec']['columns']))+'502'
        result=self.fetch(self.clients[job['app']],'GET','/open-apis/sheets/v2/spreadsheets/'+job['token']+'/values/'+urllib.parse.quote(area,safe='!:')+'?valueRenderOption=UnformattedValue')
        guard();self.leaf.load(ctx,job['report'])
        raw=encoded(result)
        original=retain(self.c,ctx,job['report'],'feishu-sheet-'+digest(raw)[:16],raw,'feishu-sheet.json','application/json','actual complete requested range '+area)
        try:
            verified=next((s['response']['valueRange']['values'][0] for s in reversed(job['steps']) if s['stage']=='verify-values'),None)
            if verified is None:raise Denied('Original published columns unavailable; preserve snapshot and resolve explicitly')
            diff=sheet_diff(saved['spec'],result['valueRange']['values'],expected_header=verified)
        except (ValueError,Denied) as error:return {'original':original,'state':'needs_review','reason':str(error),'applied':False}
        return {'original':original,'publication':publication,'revision':result.get('revision'),'state':'saved','diff':diff}
