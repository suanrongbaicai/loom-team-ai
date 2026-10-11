"""Publish retained interactive reports; the model never handles hosting credentials."""
import base64
import gzip
import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from .control import digest,identifier
from .policy import Denied,Policy,instant,utcnow
from .originals import retain
from .report_leaf import encoded


class WebUnavailable(RuntimeError):
    pass


class Publisher:
    def __init__(self,config):
        origin=config['origin'].rstrip('/')
        u=urllib.parse.urlsplit(origin)
        if u.scheme!='https' or u.path or u.query or u.fragment or u.username or u.password or not u.hostname:
            raise ValueError('Fixed HTTPS report origin required')
        self.origin=origin
        self.key=Path(config['key_file']).read_text(encoding='utf-8-sig').strip()
        if len(self.key)<40:raise ValueError('Report publisher key too short')
        proxies={'http':config['proxy'],'https':config['proxy']} if config.get('proxy') else {}
        self.http=urllib.request.build_opener(urllib.request.ProxyHandler(proxies),NoRedirect())

    def request(self,method,ident,body=None):
        identifier(ident)
        req=urllib.request.Request(self.origin+'/api/reports/'+ident,data=encoded(body) if body is not None else None,
            method=method,headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json',
                                   'User-Agent':'ANC-ReportPublisher/1.0','Accept':'application/json'})
        try:
            with self.http.open(req,timeout=25) as response:raw=response.read(256000)
            return json.loads(raw)
        except urllib.error.HTTPError as error:
            if error.code==404 and method=='GET':return {'state':'absent'}
            raise WebUnavailable('交互报表发布未确认，请使用原发布编号查状态，不要新建副本。') from None
        except Exception:raise WebUnavailable('交互报表服务暂不可达，飞书副本与库内原件仍保留。') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def grant_deadline(policy,ctx,resource):
    """Latest surviving grant per recipient, then earliest limit across recipients."""
    now=instant(utcnow());limits=[]
    for person in policy.recipients(ctx):
        if resource.scope==ctx.scope and person in resource.audience:continue
        if resource.owner==person and policy.state['scopes'][resource.scope]['kind']=='private':continue
        candidates=[]
        for g in policy.state.get('grants',[]):
            single=Policy({**policy.state,'grants':[g]})
            if single._person_allowed(person,ctx.scope,resource):
                candidates.append(instant(g['expires_at']) if g.get('expires_at') else None)
        if not candidates:raise Denied('Published report audience lacks current source permission')
        if None not in candidates:limits.append(max(candidates))
    result=min(limits) if limits else None
    if result and result<=now:raise Denied('Report source permission expired')
    return int(result.timestamp()) if result else None


class WebReports:
    def __init__(self,cloud,publisher):
        self.cloud,self.c,self.leaf,self.publisher=cloud,cloud.c,cloud.leaf,publisher

    def _save(self,ctx,ident,body):
        with self.c.db.transaction() as db:
            self.c._check(db,ctx)
            db.execute('INSERT OR REPLACE INTO web_publications VALUES(?,?,?,?)',(ident,ctx.scope,body['manifest']['report'],encoded(body).decode()))
            self.c.audit(db,ctx.principal,ctx.task,'report.web_state',{'publication':ident,'state':body['state'],'report':body['manifest']['report'],'backup':body['backup']})

    def _verified(self,result,body):
        manifest=body['manifest'];ident=manifest['id']
        if result.get('id')!=ident or result.get('state')!='published' or result.get('url')!=self.publisher.origin+'/r/'+ident:
            raise WebUnavailable('交互链接尚未通过实际回读核对。')
        # Server JSON object order is immaterial; compare the complete manifest and saved bytes digest.
        if result.get('manifest')!=manifest:raise WebUnavailable('交互版的版本或接收范围不一致，尚未交付。')
        return {'publication':ident,'state':'published','format':'interactive','report':manifest['report'],
                'version':manifest['version'],'url':result['url'],'backup_url':manifest['backup']['url'],
                'links':{'interactive':result['url'],'feishu':manifest['backup']['url']},'receipt':body.get('receipt')}

    def _finish(self,ctx,ident,body,result):
        out=self._verified(result,body)
        if not body.get('receipt'):
            body['receipt']=retain(self.c,ctx,body['manifest']['report'],'web-receipt-'+ident[4:20],encoded(result),
                'web-receipt.json','application/json','actual hosting readback before link delivery')
        body['state']='published';self._save(ctx,ident,body);out['receipt']=body['receipt']
        return out

    def publish(self,ctx,request_id,report,backup,guard):
        identifier(request_id);identifier(backup)
        saved=self.leaf.load(ctx,report)
        app,people=self.cloud.binding(ctx)
        job=self.cloud._job(ctx,backup)
        if job['state']!='published' or job['report']!=report or job['app']!=app or job['people']!=people or job['format'] not in ('docx','sheet'):
            raise Denied('需要同一报表版本、同一接收范围且已经核验的飞书副本。')
        files=self.leaf.files(ctx,report);meta,raw=files['report-html']
        if digest(raw)!=meta['sha256']:raise Denied('Retained report bytes differ')
        ident='web-'+digest((ctx.scope+':'+request_id).encode())
        with self.c.db.transaction() as db:
            p=self.c._check(db,ctx);tenant=p.state['scopes'][ctx.scope]['transport']['tenant']
            limits=[]
            refs={r['id'] for row in saved['spec']['rows'] for r in row['evidence']}|{report}
            for source in refs:
                _,resource=self.c._document(db,source);p.require_read(ctx,resource)
                limit=grant_deadline(p,ctx,resource)
                if limit is not None:limits.append(limit)
            old=db.execute('SELECT body FROM web_publications WHERE id=? AND scope=?',(ident,ctx.scope)).fetchone()
        manifest={'schema':'anc-web-report-v1','id':ident,'report':report,'version':saved['version'],
            'title':saved['spec']['title'],'app':app,'tenant':tenant,'people':sorted(people.values()),
            'html_sha256':digest(raw),'html_bytes':len(raw),'expires':min(limits) if limits else None,
            'backup':{'url':job['url'],'token':job['token'],'format':job['format']}}
        compressed=gzip.compress(raw,mtime=0)
        if len(compressed)>1500000 or len(raw)>8*1024*1024:raise ValueError('交互报表超过当前发布大小，需要按业务范围拆分；原件与飞书副本保留。')
        body=json.loads(old[0]) if old else {'manifest':manifest,'backup':backup,'state':'prepared','created':utcnow()}
        was_published=bool(old) and body.get('state')=='published'
        if body['manifest']!=manifest or body['backup']!=backup:raise Denied('同一发布编号的版本或受众发生变化，请保留原记录。')
        def check():
            guard();self.leaf.load(ctx,report)
            if self.cloud.binding(ctx)!=(app,people):raise Denied('交互报表的接收范围已改变。')
        check()
        if not old:
            body['intent']=retain(self.c,ctx,report,'web-plan-'+digest(request_id.encode())[:16],encoded(manifest),'web-intent.json','application/json','complete interactive copy version, digest and audience')
            self._save(ctx,ident,body)
        # Always inspect the deterministic URL first, including after a lost PUT response.
        result=self.publisher.request('GET',ident);check()
        if result.get('state')=='revoked':raise Denied('该分享已撤销，不能用重试恢复。')
        if result.get('state')=='absent':
            body['state']='unknown';self._save(ctx,ident,body);check()
            self.publisher.request('PUT',ident,{'manifest':manifest,'gzip':base64.b64encode(compressed).decode()})
            check();result=self.publisher.request('GET',ident);check()
        out=self._finish(ctx,ident,body,result)
        if was_published:
            out=dict(out,reused=True,created=body.get('created'),meaning='这不是新做的：同一份报表版本、同一接收范围已在%s（UTC）发布过，返回的是原来的链接。答复里必须明说“这是之前做过的那份（做于…）”，不要说成刚做好；如果需要新版，要改数据或要求重新生成。' % body.get('created'))
        return out

    def status(self,ctx,ident,guard):
        identifier(ident)
        with self.c.db.transaction() as db:
            self.c._check(db,ctx);row=db.execute('SELECT body FROM web_publications WHERE id=? AND scope=?',(ident,ctx.scope)).fetchone()
        if not row:raise Denied('Publication unavailable')
        body=json.loads(row[0]);self.leaf.load(ctx,body['manifest']['report']);guard()
        def audience():
            app,people=self.cloud.binding(ctx)
            if app!=body['manifest']['app'] or sorted(people.values())!=body['manifest']['people']:
                raise Denied('交互报表的接收范围已改变。')
        audience()
        result=self.publisher.request('GET',ident);guard();self.leaf.load(ctx,body['manifest']['report'])
        audience()
        if result.get('state')=='published':return self._finish(ctx,ident,body,result)
        return {'publication':ident,'state':result.get('state','unknown'),'backup_url':body['manifest']['backup']['url']}
