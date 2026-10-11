"""Capability requests reuse ANC identity, source records and saved human replies.

The registry is not a permission grant. Unknown integrations remain actionable
requests; only protected, configured interfaces can perform host operations.
"""
import contextlib
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import uuid

from .control import digest, identifier, within, write_durable
from .human_messages import accepted_text, context, queue, replied_action
from .policy import Denied, instant, utcnow
from .wechat_consent import account_for
from .capability_projects import Projects


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def safe_name(value):
    if (not isinstance(value,str) or not value or len(value)>160 or '\\' in value or ':' in value
            or any(p in ('','.', '..') for p in value.split('/'))
            or PurePosixPath(value).is_absolute() or any(x.endswith((' ','.')) for x in value.split('/'))
            or any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?',p) for p in value.split('/'))):
        raise Denied('项目文件名不安全。')
    return value


def permissions(value):
    if not isinstance(value,dict) or set(value)-{'interfaces','network','dependencies'}:
        raise ValueError('请明确interfaces、network和dependencies，未知能力也可列入interfaces。')
    result={key:value.get(key,[]) for key in ('interfaces','network','dependencies')}
    if any(not isinstance(v,list) or len(v)>8 for v in result.values()):
        raise ValueError('每类最多列8项明确需求。')
    from .capability_network import check_url
    names=[]
    for item in result['interfaces']:
        if not isinstance(item,dict) or not isinstance(item.get('kind'),str):
            raise ValueError('接口需要明确kind。')
        if item['kind']=='wechat.self':
            if set(item)!={'kind','account'}:raise ValueError('微信本人资料接口只需绑定账号。')
            identifier(item['account'])
        elif set(item)!={'kind','description'} or not isinstance(item['description'],str) or not item['description'].strip():
            raise ValueError('未接通的接口请写明description，包括对象及需要读写的内容。')
    for item in result['network']:
        if (not isinstance(item,dict) or set(item)-{'name','url','method','body','content_type','purpose'}
                or not item.get('purpose') or item.get('method') not in ('GET','POST')):
            raise ValueError('联网请求需要name、url、method和用途；外发正文也须明确。')
        identifier(item['name']); names.append(item['name']); check_url(item['url'])
        if not isinstance(item.get('body',''),str) or len(item.get('body','').encode())>256*1024:
            raise ValueError('外发正文超限。')
        if item['method']=='GET' and item.get('body'):raise ValueError('GET不接受正文。')
        if item.get('content_type','application/json') not in ('application/json','text/plain','application/x-www-form-urlencoded'):
            raise ValueError('请求类型需使用已接通格式。')
    if len(names)!=len(set(names)):raise ValueError('联网请求名称不能重复。')
    for item in result['dependencies']:
        if (not isinstance(item,dict) or set(item)!={'name','version','url','sha256','license'}
                or not re.fullmatch('[0-9a-f]{64}',item.get('sha256','')) or not item.get('license')):
            raise ValueError('依赖需固定name、version、url、sha256和license。')
        p=check_url(item['url'])
        if p.hostname!='files.pythonhosted.org' or not p.path.endswith('.whl'):
            raise ValueError('自动安装入口目前接受官方PyPI wheel；其他包提交接口接入需求。')
    if len(canonical(result).encode())>32000:raise ValueError('能力申请过大，请拆成有明确结果的工作。')
    return result


class Capabilities(Projects):
    def __init__(self,control,config,commands=None,collector=None):
        self.c,self.config,self.commands,self.collector=control,config,commands,collector
        if config.get('enabled') is not True or not config.get('approval'):
            raise Denied('能力接续尚未由管理入口启用。')
        self.root=within(control.root,'Capabilities'); self.root.mkdir(exist_ok=True)
        self.path=self.root/'state.sqlite'
        self.projects=Path(config['projects_root']).absolute(); self.tools=Path(config['tools_root']).absolute()
        for path in (self.projects,self.tools):
            if path.drive.lower()=='c:':raise Denied('项目与工具发布放在获准的非C盘位置。')
            within(path.parent,path.name); path.mkdir(parents=True,exist_ok=True)
        with self.db() as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0,1):raise Denied('能力登记版本不兼容，保留原件。')
            db.execute('CREATE TABLE IF NOT EXISTS objects(id TEXT PRIMARY KEY,kind TEXT NOT NULL,actor TEXT NOT NULL,scope TEXT NOT NULL,body TEXT NOT NULL)')
            db.execute('PRAGMA user_version=1')

    @contextlib.contextmanager
    def db(self):
        with contextlib.closing(sqlite3.connect(self.path,timeout=10)) as db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA synchronous=FULL')
            with db:yield db

    def put(self,kind,key,ctx,body):
        with self.db() as db:
            db.execute('INSERT INTO objects VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                       (key,kind,ctx.principal,ctx.scope,canonical(body)))

    def get(self,ctx,key,kind=None):
        identifier(key)
        with self.db() as db:
            row=db.execute('SELECT * FROM objects WHERE id=?',(key,)).fetchone()
        if not row or (row['actor'],row['scope'])!=(ctx.principal,ctx.scope) or (kind and row['kind']!=kind):
            raise Denied('这项工具或申请不在当前身份及会话范围。')
        return json.loads(row['body'])

    def current(self,ctx,writable=False):
        with self.c.db.transaction() as db:
            policy=self.c._check(db,ctx,writable)
            rule=self.config.get('rules',{}).get(ctx.principal)
            if (not rule or not rule.get('active') or not rule.get('approval')
                    or ctx.scope not in rule.get('scopes',[]) or rule.get('approver')!=ctx.principal):
                raise Denied('当前身份的技术批准路径尚未登记；保留原任务，明确需要谁批准，不要自称管理员。')
            if rule.get('expires_at') and instant(rule['expires_at'])<=datetime.now(timezone.utc):
                raise Denied('技术批准规则已到期。')
            return policy,rule

    def stamp(self,ctx,spec):
        policy,rule=self.current(ctx)
        bindings=[]
        for item in spec['interfaces']:
            if item['kind']=='wechat.self':bindings.append(account_for(policy,ctx,item['account']))
        return {'rule':digest(canonical(rule).encode()),'identity':policy.state['members'][ctx.principal].get('bindings',[]),
                'audience':sorted(policy.recipients(ctx)),'account_bindings':bindings}

    def checked(self,ctx,key,writable=True):
        self.current(ctx,writable)
        row=self.get(ctx,key,'request')
        if row['state'] not in ('approved','needs_integration') or not row.get('approval'):
            raise Denied('这项能力还未获得有效批准，或已停用。')
        if self.stamp(ctx,row['permissions'])!=row['stamp']:
            raise Denied('身份、账号、受众或批准规则已变，原批准不能套用。')
        return row

    def audit(self,ctx,action,value):
        with self.c.db.transaction() as db:
            self.c._check(db,ctx)
            self.c.audit(db,ctx.principal,ctx.task,'capability.'+action,value)

    def save_evidence(self,ctx,key,body):
        text=canonical(body)
        self.c.register(ctx,key,'data')
        with self.c.db.transaction() as db:
            doc=db.execute('SELECT * FROM documents WHERE id=?',(key,)).fetchone()
            if doc['revision']:
                if self.c._read(db,doc).decode()!=text:raise Denied('证据编号已被不同结果使用。')
                return key
        self.c.commit(ctx,key,0,text,'commit-'+digest(key.encode())[:48],self.c.lease(ctx,key))
        return key

    def catalog(self,ctx):
        self.current(ctx)
        with self.db() as db:
            rows=db.execute("SELECT id,kind,body FROM objects WHERE actor=? AND scope=? AND kind IN ('request','build') ORDER BY rowid DESC LIMIT 80",(ctx.principal,ctx.scope)).fetchall()
        found=[]
        for row in rows:
            item=json.loads(row['body'])
            if row['kind']=='request':
                try:
                    if item.get('stamp')!=self.stamp(ctx,item['permissions']):continue
                except Denied:continue
            if row['kind']=='build':
                try:self.checked(ctx,item['request'],False)
                except Denied:continue
            found.append({'id':row['id'],'kind':row['kind'],'title':item.get('title'),
                          'state':item['state'],'version':item.get('version'),'request':item.get('request'),
                          'permissions':item.get('permissions'),'source':item.get('source')})
        return {'items':found,'development':'可在任务目录编程；持久项目经capability_save/validate/publish/call处理。',
                'native_integrations':self.config.get('native_integrations', {}),
                'interfaces':['wechat.self','approved HTTPS GET/POST','approved PyPI pure-Python wheels'],
                'unknown':'登记不是能力上限。其他接口也可提出具体申请，批准后保留工程接入任务及原任务关系；不要把未接入当永久禁止。'}

    def request(self,ctx,id,title,purpose,permissions,approach):
        from .capabilities import permissions as normalize
        spec=normalize(permissions); identifier(id)
        if any(not isinstance(v,str) or not 1<=len(v.strip())<=4000 for v in (title,purpose,approach)):
            raise ValueError('请写明目标、用途、已查办法和具体接入方法。')
        policy,rule=self.current(ctx,True)
        key='cap-'+digest((ctx.scope+':'+ctx.principal+':'+id).encode())[:48]
        wanted={'title':title,'purpose':purpose,'approach':approach,'permissions':spec,'stamp':self.stamp(ctx,spec)}
        with self.c.lock:
            try:old=self.get(ctx,key,'request')
            except Denied:old=None
            if old:
                if any(old[k]!=v for k,v in wanted.items()):raise Denied('同一申请编号不能更换范围，请提交新版本。')
                return self._present(ctx,key,old)
            approved=None
            # Standing grants are installed only by the protected operator with
            # the user's evidence, never learned from a model or a tool result.
            for grant in rule.get('grants',[]):
                if grant.get('approval') and all(all(v in grant.get(k,[]) for v in spec[k]) for k in spec):
                    approved={'kind':'standing','evidence':grant['approval'],'at':utcnow()}; break
            row=dict(wanted,creator_task=ctx.task,created=utcnow(),state='approved' if approved else 'pending',approval=approved)
            if approved and any(i['kind']!='wechat.self' for i in spec['interfaces']):row['state']='needs_integration'
            self.put('request',key,ctx,row)
            self.save_evidence(ctx,'request-'+key,dict(wanted,id=key,created=row['created'],approval=approved))
            self.audit(ctx,'requested',{'id':key,'state':row['state']})
            return self._present(ctx,key,row)

    def _present(self,ctx,key,row):
        if row['state']!='pending':
            next_step={'approved':'已有批准覆盖，直接开发、保存、验证、发布并继续原任务。',
                       'needs_integration':'保留此申请，编写接入项目和验证办法；需管理端接通该宿主接口，不能把它当永久禁止。',
                       'revoked':'这项授权已停止，不得继续使用；保留代码、原任务和历史记录。'}
            return {'id':key,'state':row['state'],'approval':row['approval'],
                    'next':next_step.get(row['state'],'请先核对申请的当前状态。')}
        with self.c.db.transaction() as db:
            prior=db.execute("SELECT o.id,o.state FROM message_actions a JOIN outbox o ON o.id=a.outbox "
                             "JOIN tasks t ON t.id=o.task WHERE a.kind='capability' AND a.target=? "
                             "AND a.version=1 AND a.scope=? AND t.actor=?",
                             (key,ctx.scope,ctx.principal)).fetchone()
        if prior:
            return {'id':key,'state':'awaiting-human','outbox':prior['id'],'delivery_state':prior['state'],
                    'instruction':'沿用已保存的确认消息，不重复创建或发送。若送达失败或结果不明，先核对投递记录。'}
        spec=row['permissions']; network=[]
        for item in spec['network']:
            network.append(item['method']+' '+item['url']+'；用途：'+item['purpose']+
                           ('；外发正文：'+item['body'] if item.get('body') else '；不发送正文'))
        interface_text='；'.join('当前已绑定微信账号的本人昵称与身份核对结果' if i['kind']=='wechat.self' else i['description'] for i in spec['interfaces'])
        text=('需要补一项能力：'+row['title']+'\n用途：'+row['purpose']+'\n准备这样办：'+row['approach']+
              '\n允许读取或操作：'+(interface_text or '本任务输入和项目文件')+
              '\n联网：'+('；'.join(network) or '无')+'\n依赖：'+
              ('；'.join(i['name']+' '+i['version']+'（'+i['url']+'）' for i in spec['dependencies']) or '现有环境')+
              '\n代码和工具放E盘获准目录；只给当前已核实接收人使用。批准包括该范围内开发、测试、安装和后续复用；不产生购买授权。'+
              ('\nPOST外发只批准一次；结果不明时先核实，不自动重发。' if any(i['method']=='POST' for i in spec['network']) else '')+
              '\n请回复本消息“同意”或“不同意”。同意后我会接着办原来的事。')
        oid=queue(self.c,ctx,'view-'+key,text,True,('capability',key,1))
        return {'id':key,'state':'awaiting-human','outbox':oid,'instruction':'这一条确认已保存排队，本轮结束，不重复解释或再问一次。'}

    def approve(self,ctx,key,original):
        # This method is not in the worker API. Require an accepted original and
        # an exact delivered parent action even when invoked by trusted code.
        with self.c.lock,self.c.db.transaction() as db:
            self.c._check(db,ctx,True)
            source=db.execute('SELECT d.* FROM tasks t JOIN messages m ON m.id=t.event JOIN documents d ON d.id=m.resource WHERE t.id=?',(ctx.task,)).fetchone()
            if not source or json.loads(self.c._read(db,source))!={k:v for k,v in original.items() if k!='_app'}:
                raise Denied('缺少可信平台批准原件。')
        action=replied_action(self.c,ctx,original)
        if not action or (action['kind'],action['target'],action['version'])!=('capability',key,1):
            raise Denied('批准没有对应已送达的当前申请。')
        row=self.get(ctx,key,'request')
        if self.stamp(ctx,row['permissions'])!=row['stamp']:raise Denied('批准依据已变化。')
        stamp=original.get('create_time','')
        if not str(stamp).isdigit() or not instant(row['created']).timestamp()<=int(stamp)/1000<=datetime.now(timezone.utc).timestamp()+60:
            raise Denied('批准消息时间不符合本次申请。')
        with self.c.db.transaction() as db:
            creator=db.execute('SELECT cancelled FROM tasks WHERE id=?',(row['creator_task'],)).fetchone()
        if not creator or creator['cancelled']:raise Denied('原任务已取消，不能继续生效。')
        if row['state'] in ('approved','needs_integration'):return row
        if row['state']!='pending':raise Denied('这份申请已停用。')
        row['approval']={'kind':'platform','message':original['message_id'],'task':ctx.task,'at':utcnow(),'version':1}
        row['state']='needs_integration' if any(i['kind']!='wechat.self' for i in row['permissions']['interfaces']) else 'approved'
        self.put('request',key,ctx,row)
        self.save_evidence(ctx,'approval-'+key,row['approval'])
        self.audit(ctx,'approved',{'id':key,**row['approval']})
        return row

    def handle(self,envelope,received):
        text,original=accepted_text(self.c,received)
        if text not in ('同意','不同意','停用'):return None
        ctx=context(self.c,received['task']); original['_app']=envelope['bot_id']
        action=replied_action(self.c,ctx,original)
        if not action or action['kind']!='capability':return None
        with self.c.lock:
          try:
            if text in ('不同意','停用'):
                row=self.get(ctx,action['target'],'request');self.current(ctx,True)
                row['state']='revoked';self.put('request',action['target'],ctx,row)
                self.audit(ctx,'revoked',{'id':action['target'],'message':original['message_id']})
                queue(self.c,ctx,'cap-stop-'+ctx.task,'已停止这项能力授权；已完成的动作和历史记录保留。')
            else:
                try:
                    row=self.approve(ctx,action['target'],original)
                    # Approval message task is already queued by ingress. Add
                    # the original task pointer; don't reset it or launch twice.
                    with self.c.db.transaction() as db:
                        self.c.audit(db,ctx.principal,ctx.task,'capability.continuation',
                                     {'request':action['target'],'parent':row['creator_task']})
                except Denied as e:
                    queue(self.c,ctx,'cap-held-'+ctx.task,str(e))
          except Denied as e:
            queue(self.c,ctx,'cap-held-'+ctx.task,str(e))
        return {'handled':True,'request':action['target']}

    def continuation(self,ctx):
        with self.c.db.transaction() as db:
            row=db.execute("SELECT detail FROM audit WHERE task=? AND action='capability.continuation' ORDER BY id DESC LIMIT 1",(ctx.task,)).fetchone()
        if not row:return ''
        item=json.loads(row[0]);self.checked(ctx,item['request'],False)
        return ('\n这是能力批准后的原任务续办：申请 '+item['request']+'，原任务 '+item['parent']+
                '。用capability_status读取申请、原件入口和工程状态；已有授权直接用，不重复确认，完成原业务后保存并回复。')

    def status(self,ctx,id):
        self.current(ctx)
        row=self.get(ctx,id)
        if 'permissions' in row and self.stamp(ctx,row['permissions'])!=row['stamp']:raise Denied('申请依据已变化。')
        if row.get('request'):self.checked(ctx,row['request'],False)
        if row.get('snapshot'):
            raw=Path(row['snapshot']).read_bytes()
            if digest(raw)!=row['digest']:raise Denied('源码版本校验失败。')
            row=dict(row,project=json.loads(raw))
        integration = self.config.get('native_integrations', {}).get(id)
        if integration:
            row = dict(row, integration=integration,
                       meaning='原申请与批准原件保留；正式接口按另列当前授权可用，不伪称历史批准或历史发布成功。')
        return row
