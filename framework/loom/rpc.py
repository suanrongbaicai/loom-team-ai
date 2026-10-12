"""Loopback controller; worker credentials bind a task, never a caller-supplied identity."""
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import uuid

from .control import Conflict
from .governance import Governance
from .ingress import Ingress
from .policy import Denied
from .rules import Rules
from .skills import Skills
from .tasks import Tasks
from .wechat import WeChat
from .wechat_capture import WeChatCapture
from .reports import Reports
from .mcp import handle as handle_mcp


class RuntimeAPI:
    def skill_read(self, ctx, key):
        """Full text of a skill deployed for THIS scope, digest re-verified, loaded-audit written by the trusted path (never by the model)."""
        from .skills import tree_digest
        with self.c.lock, self.c.db.transaction() as db:
            self.c._check(db, ctx)
            deployed = db.execute('SELECT * FROM skill_deployments WHERE scope=? AND skill=?', (ctx.scope, key)).fetchone()
            pinned = db.execute('SELECT version FROM skill_uses WHERE task=? AND skill=?', (ctx.task, key)).fetchone()
            version = pinned[0] if pinned else (deployed['version'] if deployed else None)
            skill = db.execute('SELECT * FROM skills WHERE id=? AND version=?', (key, version)).fetchone() if version else None
            if not deployed or not skill or skill['state'] != 'active':
                raise Denied('Skill is not deployed for this scope')
            if tree_digest(skill['path'])[0] != skill['digest']:
                raise Denied('Published method content changed')
            text = (Path(skill['path']) / 'SKILL.md').read_text(encoding='utf-8')
            db.execute('INSERT OR IGNORE INTO skill_uses VALUES(?,?,?,?)', (ctx.task, key, version, 'loaded'))
            db.execute("UPDATE skill_deployments SET stage='loaded' WHERE scope=? AND skill=? AND version=?", (ctx.scope, key, version))
            already = any((json.loads(r[0]).get('skill'), json.loads(r[0]).get('digest')) == (key, skill['digest']) for r in
                          db.execute("SELECT detail FROM audit WHERE task=? AND action='skill.loaded'", (ctx.task,)))
            if not already:
                self.c.audit(db, 'trusted-hook', ctx.task, 'skill.loaded', {'skill': key, 'version': version, 'digest': skill['digest']})
        return {'skill': key, 'version': version, 'digest': skill['digest'], 'text': text}

    def __init__(self, control, framework, transport_secret, verify_audience=None, commands=None, collector=None, images=None, engineering=None, report_leaf=None, cloud_reports=None, public_web=None, personnel_notices=None, contacts=None, generated_images=None, group_history=None):
        self.c = control
        self.rules = Rules(control, framework)
        self.ingress = Ingress(control)
        self.transport_secret = transport_secret
        self.capabilities = {}
        self.cap_lock = threading.Lock()
        self.verify_audience = verify_audience
        self.commands = commands
        self.images = images
        self.generated_images = generated_images
        self.group_history = group_history
        self.engineering = engineering
        self.contacts = contacts
        self.report_leaf = report_leaf
        self.cloud_reports = cloud_reports
        self.public_web = public_web
        self.personnel_notices = personnel_notices
        self.wechat_capture = WeChatCapture(control, collector)
        from .wechat_attachments import Attachments
        self.wechat_attachments = Attachments(control, collector) if collector else None

    def observe_unknown(self, envelope):
        if self.personnel_notices:
            from .unknown_contact import observe
            observe(self.c, envelope, self.personnel_notices)

    def accept(self, envelope, _observed=False):
        if not _observed:
            self.observe_unknown(envelope)
        if self.verify_audience:
            source = envelope['source']
            header, message = source['header'], source['event']['message']
            with self.c.db.transaction() as db:
                state = self.c.db.get_policy(db)
                scopes = [sid for sid, scope in state['scopes'].items()
                          if scope.get('transport', {}).get('chat') == message['chat_id']
                          and scope['transport'].get('tenant') == header.get('tenant_key')
                          and envelope['bot_id'] in scope['transport'].get('apps', [])]
            if len(scopes) != 1:
                raise Denied('Chat binding unavailable or ambiguous')
            if self.verify_audience(scopes[0]) is not True:
                raise RuntimeError('Current audience unavailable')
        from .platform_decisions import handle as human_decision
        from .platform_tasks import handle as human_task
        from .wechat_consent import handle as capture_consent
        # Keep explicit control messages from racing the model scheduler.
        with self.c.lock:
            result = self.ingress.accept(envelope)
            if self.engineering:
                engineering=self.engineering.handle(envelope,result)
                if engineering:
                    result['capability_decision']=engineering
                    return result
            consent = capture_consent(self.c, envelope, result)
            if consent:
                result['wechat_consent'] = consent
                return result
            decision = human_decision(self.c, envelope, result)
            if decision:
                result['human_decision'] = decision
            else:
                from .unknown_contact import handle as personnel_decision
                recorded = personnel_decision(self.c, envelope, result)
                if recorded:
                    result['personnel_decision'] = recorded
                    return result
                task_control = human_task(self.c, envelope, result)
                if task_control:
                    result['human_task'] = task_control
            return result

    def issue(self, ctx):
        if self.verify_audience and self.verify_audience(ctx.scope) is not True:
            raise RuntimeError('Current audience unavailable')
        with self.c.db.transaction() as db:
            self.c._check(db, ctx)
        token = secrets.token_urlsafe(48)
        with self.cap_lock:
            self.capabilities[hashlib.sha256(token.encode()).digest()] = ctx
        return token

    def revoke(self, token):
        with self.cap_lock:
            self.capabilities.pop(hashlib.sha256(token.encode()).digest(), None)

    def context(self, token, cancelling=False):
        with self.cap_lock:
            ctx = self.capabilities.get(hashlib.sha256(token.encode()).digest())
        if ctx is None:
            raise Denied('Task credential unavailable')
        if not cancelling:
            if self.verify_audience and self.verify_audience(ctx.scope) is not True:
                raise RuntimeError('Current audience unavailable')
            with self.c.db.transaction() as db:
                self.c._check(db, ctx)
        return ctx

    def closing_text_guard(self, ctx, finish):
        """A file must not be the last word (T2-4, 0153): delivering a report with finish=true while this task has sent no text of its own
        ends the task with the file alone, and the model's final plain message is never delivered.  Refused BEFORE anything is saved."""
        if finish is not True:
            return
        with self.c.db.transaction() as db:
            # System notices ("still working", "stop?") use the same outbox but are not the model's own words.
            # Counted: a text whose send is still pending or was attempted ('queued','sending','sent') or whose outcome is unknown ('unknown': it must
            # not be re-sent, so refusing the file would only trap the model).  NOT counted: texts known not to go out ('cancelled','delivery_failed',
            # 'needs_reauthorization').  Counting a queued text never claims delivery: the tools still answer 'queued-not-delivered', and a later
            # failure/unknown outcome is handled by the usual send/reconcile path.
            text = db.execute('SELECT 1 FROM outbox o LEFT JOIN file_replies f ON f.reply_document=o.payload_document '
                              "WHERE o.task=? AND f.reply_document IS NULL AND o.payload_document NOT IN (?,?) AND o.state IN ('queued','sending','sent','unknown') LIMIT 1",
                              (ctx.task, 'working-' + ctx.task, 'human-stop-' + ctx.task)).fetchone()
        if not text:
            raise ValueError('文件不能是这一轮的最后一句：没有保存任何东西。请先把本次文件交付设为 finish=false（或先用 reply 发一段说明，finish=false），'
                             '文件发出后再用 save_work.reply 或 reply 写一段文字收尾（说明文件里是什么、要点和下一步，finish=true）。')

    def call(self, token, method, args):
        ctx = self.context(token, cancelling=method == 'cancel')
        if method == 'cancel':
            return self.c.cancel(ctx)
        if method == 'preflight':
            text = self.rules.load(ctx, 'WorkerPreflight')
            return {'complete': bool(text)}
        if method == 'rules':
            return {'text': self.rules.load(ctx, args['event'], args.get('session'))}
        with self.c.db.transaction() as db:
            if not db.execute("SELECT 1 FROM audit WHERE task=? AND action='rules.delivered' LIMIT 1", (ctx.task,)).fetchone():
                raise Denied('Current task has no confirmed full rules delivery')
        if method == 'group_history':
            if self.group_history is None:
                raise Denied('群历史平台授权或按需入口尚未接通，不能声称已读取。')
            return self.group_history.query(ctx, args, lambda: self.context(token))
        if method in ('member_find', 'contact_private', 'contact_status'):
            if self.contacts is None:
                raise Denied('主动私聊接口尚未接通；保留工作并提出具体缺口。')
            allowed = {'member_find': {'query', 'group', 'limit'},
                       'contact_private': {'id', 'messages', 'purpose', 'mode', 'basis', 'group', 'exclude'},
                       'contact_status': {'id'}}[method]
            if set(args) - allowed:
                raise Denied('通信工具不接受机器人、账号、路径或任意接口参数。')
            operation = {'member_find': self.contacts.find, 'contact_private': self.contacts.contact,
                         'contact_status': self.contacts.status}[method]
            result = operation(ctx, **args)
            self.context(token)
            return result
        if method in ('generated_images', 'image_reference', 'send_image', 'image_status'):
            if self.generated_images is None:
                raise Denied('本 bot 的图片生成交付尚未接通，不能声称已生成或已发出。')
            if method == 'generated_images':
                if set(args)-{'scope','limit'}: raise Denied('图片目录只接受task/history范围和limit，不接受路径、账号或其他任务。')
                return self.generated_images.list(ctx,**args)
            if method == 'image_reference':
                if set(args)-{'image'}: raise Denied('原图引用只接受目录image或本轮真实回复父消息，不接受路径、bot或任意消息。')
                return self.generated_images.reference(ctx,**args)
            if method == 'image_status':
                if set(args) != {'id'}: raise Denied('只能核对本任务的稳定交付编号。')
                return self.generated_images.status(ctx, args['id'])
            if set(args) - {'image', 'reference', 'id', 'recording', 'finish'}:
                raise Denied('图片交付不接受收件人、bot、路径或原始图片参数。')
            return self.generated_images.queue(ctx, args.get('image'), args['id'], args.get('recording'), args.get('finish', False),args.get('reference'))
        if method == 'guard':
            return {'current': True}
        if method in ('web_search','web_read'):
            if self.public_web is None:raise Denied('公开网页工具尚未配置，不能假称已经查网。')
            result=self.public_web.run(ctx,'search' if method=='web_search' else 'read',args,lambda:self.context(token))
            self.context(token)
            return result
        if method.startswith('capability_'):
            if self.engineering is None:
                raise Denied('能力接续服务尚未接通；保留原任务并提出具体接入需求。')
            e=self.engineering
            guard=lambda:self.context(token)
            if method=='capability_catalog':result=e.catalog(ctx)
            elif method=='capability_request':result=e.request(ctx,args['id'],args['title'],args['purpose'],args['permissions'],args['approach'])
            elif method=='capability_status':result=e.status(ctx,args['id'])
            elif method=='capability_save':result=e.save(ctx,args['request'],args['version'],args['files'],args['entrypoint'],args['cases'])
            elif method=='capability_validate':result=e.validate(ctx,args['build'],guard)
            elif method=='capability_publish':result=e.publish(ctx,args['build'])
            elif method=='capability_call':result=e.call_program(ctx,args['build'],args['id'],args.get('arguments',{}),guard)
            elif method=='capability_fetch':result=e.fetch(ctx,args['request'],args['name'],guard)
            else:raise Denied('Capability operation not exposed')
            self.context(token)
            return result
        if method in ('report_templates','report_view','report_build','report_publish','report_cloud_status','report_import','cloud_copy'):
            if method=='report_templates':
                from .report_templates import catalog
                return catalog()
            if self.report_leaf is None:raise Denied('报表生成器尚未配置')
            if method=='report_view':return self.report_leaf.save(ctx,args['id'],args['spec'],args.get('previous'))
            if self.cloud_reports is None:raise Denied('飞书原生报表尚未接通')
            def report_guard():self.context(token);return True
            if method=='report_build':
                from .report_build import build
                return build(self.report_leaf,self.cloud_reports,ctx,args['id'],args['spec'],
                             args['backup_format'],report_guard,args.get('previous'))
            if method=='report_publish':
                if args['format']=='interactive':
                    web=getattr(self.cloud_reports,'web',None)
                    if web is None:raise Denied('交互网页发布尚未接通；库内HTML已保存，飞书副本可单独使用。')
                    if not args.get('backup'):raise Denied('请关联同版本已发布的飞书副本凭据。')
                    return web.publish(ctx,args['id'],args['report'],args['backup'],report_guard)
                return self.cloud_reports.publish(ctx,args['id'],args['report'],args['format'],report_guard)
            if method=='report_cloud_status':
                if args['publication'].startswith('web-'):
                    web=getattr(self.cloud_reports,'web',None)
                    if web is None:raise Denied('交互网页发布尚未接通。')
                    return web.status(ctx,args['publication'],report_guard)
                return self.cloud_reports.status(ctx,args['publication'])
            if method=='cloud_copy':
                from .cloud_copy import CloudCopy
                return CloudCopy(self.cloud_reports).run(ctx,args['id'],args['url'],args['source'],report_guard)
            return self.cloud_reports.import_sheet(ctx,args['publication'],report_guard)
        if method == 'execute':
            if self.commands is None:
                raise Denied('Command executor is not provisioned')
            result = self.commands.execute(ctx, args['command'], args.get('timeout_seconds', 60))
            self.context(token)  # Never return output after cancellation or audience change.
            return result
        if method == 'tasks':
            return Tasks(self.c).list(ctx, args.get('task'))
        if method == 'new_messages':
            from .supplements import collect
            return collect(self.c, ctx)
        if method == 'set_my_profile':
            from .member_labels import set_own_profile
            return set_own_profile(self.c, ctx, args.get('call_name'), args.get('about'), args.get('preferences'))
        if method == 'set_group_alias':
            from .group_settings import set_alias
            return set_alias(self.c, ctx, args.get('alias'))
        if method == 'image':
            if self.images is None:
                raise Denied('Image source connector is not provisioned')
            result = self.images.read(ctx, args['source'])
            self.context(token)
            return result
        if method == 'materials':
            from .originals import find
            return {'originals':find(self.c,ctx,args['source']),
                    'coverage':'已实际留存原件；空列表表示尚未留存，不表示来源没有附件。'}
        if method == 'file':
            if self.images is None:raise Denied('File source connector unavailable')
            result=self.images.file(ctx,args['source']);self.context(token);return result
        if method == 'material':
            from .originals import get
            from .wechat_media_reader import text_file
            from pathlib import Path
            meta,raw,reference=get(self.c,ctx,args['id'],with_reference=True)
            try:text=text_file(raw,Path(meta['name']).suffix.lower())
            except (ValueError,UnicodeError):text=None
            return {'original':meta,'text':text,'citation':reference,
                    'citation_usage':'若实际采用本文件，用此citation连接固定原件，并在CL说明采用的内容及本次读取范围；只是读过不用引用。文件指纹定位原件，不冒充正文摘录。',
                    'reading':'text extraction only; formatting/media not read' if text is not None else 'original retained; this format not decoded by this reader'}
        if method == 'wechat_sources':
            return WeChat(self.c).sources(ctx)
        if method == 'wechat_reuse':
            return WeChat(self.c).reuse(ctx, args['source'])
        if method == 'wechat_capture_targets':
            from .wechat_consent import CaptureConsent
            result = self.wechat_capture.targets(ctx)
            result['requestable_accounts'] = CaptureConsent(self.c).accounts(ctx)
            return result
        if method == 'wechat_capture_request':
            from .wechat_consent import CaptureConsent
            if self.wechat_capture.collector is None:
                raise Denied('微信采集服务尚未配置。')
            return CaptureConsent(self.c).request(ctx, request_id=args['id'], account=args['account'],
                start=args['start'], end=args['end'], purpose=args['purpose'], selector=args['selector'])
        if method == 'wechat_capture':
            def current():
                self.context(token)
                return True
            return self.wechat_capture.capture(ctx, args['id'], args['target'], args['start'],
                                               args['end'], args['purpose'], current)
        if method == 'wechat_read':
            return self.wechat_capture.read_page(ctx, args['source'], args.get('offset', 0), args.get('limit', 200))
        if method == 'wechat_attachment':
            if self.wechat_attachments is None:
                raise Denied('附件读取服务尚未接通。')
            def attachment_guard():
                self.context(token)
                return True
            try:
                return self.wechat_attachments.read(ctx, args['source'], args['message_index'], attachment_guard,args.get('frame_times'))
            except (ValueError, RuntimeError) as error:
                self.context(token)
                raise Denied('这条附件尚未读到。请保留未完成状态，继续处理已读资料；不要猜测附件内容或重采全部消息。') from None
        if method == 'mobile_report':
            return Reports(self.c).generate(ctx, args['id'], args['source'], args['title'], args['items'], args.get('previous'))
        if method == 'record_report':
            from .record_reports import report_shape_problem
            problem = report_shape_problem(args)
            if problem:
                raise ValueError('record_report 未保存：' + problem)
            saved = Reports(self.c).from_records(ctx, args['id'], args['title'], args['items'], args.get('previous'))
            return dict(saved, next='已保存，尚未发送。要交付这份表：用 save_work.report（新的交付 id、report=上面的编号、recording）或 send_report；文字回复不会带上文件，没发送前不要说表已发送。')
        if method == 'send_report':
            self.closing_text_guard(ctx, args.get('finish', True))
            return Reports(self.c).queue(ctx, args['report'], args['id'], args.get('recording'), args.get('finish', True))
        if method == 'cancel_task':
            return Tasks(self.c).cancel(ctx, args['task'])
        if method == 'background_task':
            return Tasks(self.c).start(ctx, args['instruction'], args.get('resume_task'), args['id'])
        if method == 'read':
            return self.c.read(ctx,args['id'],args.get('revision'),args.get('start'),args.get('max_chars'))
        if method == 'case_passage':
            from .references import passage
            return passage(self.c,ctx,args['event'],args['base'],args['text'],args['commit_id'],args.get('citations',[]),args.get('take_over',False))
        if method == 'citations':
            from .references import occurrences,counts
            found=occurrences(self.c,ctx,args.get('source'),args.get('event'),args.get('revision'),args.get('quote'))
            offset=args.get('offset',0)
            if type(offset) is not int or offset<0:raise ValueError('Invalid citation offset')
            grouped=counts(found)
            return {'total':len(found),'occurrences':found[offset:offset+80],
                    'counts_by_segment':grouped[:80],'counts_truncated':len(grouped)>80,
                    'next_offset':offset+80 if offset+80<len(found) else None,
                    'coverage':'仅当前全体接收人可读、且已保存的逐次采用；不含旧版未登记采用。'}
        if method == 'impact':
            from .references import impact
            return impact(self.c,ctx,args['source'],args.get('revision'),args.get('after',0))
        if method == 'action_log':
            from .record_actions import read
            return read(self.c,ctx,args['recording'],args.get('after',0))
        if method == 'case_catalog':
            from .references import catalog
            return catalog(self.c,ctx,args.get('limit',80),args.get('offset',0),args.get('snapshot'))
        if method == 'objects':
            from .retrieval import objects
            return objects(self.c,ctx,args.get('query'),args.get('limit',30),args.get('offset',0),args.get('snapshot'))
        if method == 'read_many':
            keys = args.get('ids')
            requests = args.get('requests')
            if (keys is None) == (requests is None):
                raise ValueError('Provide ids or requests, not both')
            if requests is not None:
                if not isinstance(requests,list) or not 1<=len(requests)<=12 or any(not isinstance(v,dict) or set(v)-{'id','revision','start','max_chars'} or 'id' not in v for v in requests):
                    raise ValueError('Read 1..12 explicit record ranges')
                keys = [v['id'] for v in requests]
            if (not isinstance(keys, list) or not 1 <= len(keys) <= 12
                    or any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys)):
                raise ValueError('Read 1..12 distinct record IDs')
            # Verify the full batch before returning any content. All reads keep
            # their normal audits and current authorization checks.
            with self.c.lock:
                with self.c.db.transaction() as db:
                    policy = self.c._check(db, ctx)
                    for key in keys:
                        _, resource = self.c._document(db, key)
                        policy.require_read(ctx, resource)
                return {'records': self.c.read_many(ctx,requests if requests is not None else [{'id':key} for key in keys])}
        if method == 'search':
            return self.c.search(ctx,args.get('query'),args.get('limit',20),
                                 queries=args.get('queries'),kinds=args.get('kinds'),
                                 offset=args.get('offset',0),snapshot=args.get('snapshot'))
        if method == 'patch_context':
            from .context_patch import patch
            return patch(self.c, ctx, args['id'], args['base'], args['edits'], args['commit_id'],
                         args['effects'], args.get('take_over', False))
        if method == 'link_context':
            from .navigation import link
            return link(self.c, ctx, args['id'], args['base'], args['target'], args['target_revision'],
                        args['commit_id'], args.get('event'), args.get('take_over', False))
        if method == 'stage_receipt':
            from .stages import inspect
            return inspect(self.c, ctx, args.get('stage'), args.get('offset', 0))
        if method == 'save_work':
            # Model still chooses the records and wording. This only removes
            # round trips; each operation keeps its own guard/audit/receipt.
            updates = args.get('updates', [])
            # An empty list is fine when the call only delivers a reply/report (same as calling reply/send_report).
            if (not isinstance(updates, list) or len(updates) > 6
                    or (not updates and args.get('reply') is None and args.get('report') is None)):
                raise ValueError('Save 1..6 explicit record updates (an empty list is allowed only together with a reply or report)')
            if len(json.dumps(args, ensure_ascii=False).encode()) > 256 * 1024:
                raise ValueError('Work batch is too large')
            for position, update in enumerate(updates):
                if not isinstance(update, dict):
                    raise ValueError('No updates saved. Each updates item must be an object with method and arguments.')
                method_name, body = update.get('method'), update.get('arguments')
                if method_name not in ('record', 'append_data','case_passage', 'patch_context', 'link_context') or not isinstance(body, dict):
                    raise ValueError('No updates saved. Each updates item must be {"method":"patch_context","arguments":{...}} '
                                     '(or method record, append_data, case_passage, link_context). Put the unchanged operation '
                                     'fields inside arguments, keep the same commit_id, and retry the batch; do not invent a new ID.')
                problem = update_shape_problem(method_name, body)
                if problem:
                    raise ValueError('No updates saved (updates[%d], method=%s): %s' % (position, method_name, problem))
            reply = args.get('reply')
            report = args.get('report')
            stray = sorted(set(args) - {'updates', 'reply', 'report'})
            if stray:
                where = ('reply' if reply is not None else 'report' if report is not None else 'reply')
                hint = (' recording 必须写在 %s 里面（%s.recording），不是与它并列。' % (where, where)) if 'recording' in stray else ''
                raise ValueError('No updates saved. save_work 只接受 updates、reply、report；多出：%s。%s' % (', '.join(stray), hint))
            if reply is not None and report is not None:
                raise ValueError('One stage delivery: choose text reply or saved report')
            if isinstance(report, dict):
                self.closing_text_guard(ctx, report.get('finish', True))      # before anything is saved: the batch is not half-done
            if reply is not None and (not isinstance(reply, dict) or not reply.get('id') or not isinstance(reply.get('text'), str)):
                raise ValueError('Exact reply and stable reply ID required')
            receipts = []
            for index, update in enumerate(updates):
                try:
                    receipts.append(self.call(token, update['method'], update['arguments']))
                except Exception as error:
                    # Files already committed are preserved, never rolled back
                    # or reported as absent. Caller reconciles this exact batch.
                    self.context(token)
                    stage = self.call(token, 'stage_receipt', {})
                    return {'state': 'partial', 'completed': receipts, 'failed_index': index,
                            'error': str(error), 'reply_queued': False, 'stage':stage['stage'],
                            'commits_by_layer':stage['commits_by_layer']}
            self.context(token)
            result = {'state': 'saved', 'completed': receipts, 'reply_queued': False}
            try:
                if reply is not None:
                    result['reply'] = self.call(token, 'reply', dict(reply, finish=reply.get('finish', True)))
                    result['reply_queued'] = True
                elif report is not None:
                    result['reply'] = self.call(token, 'send_report', report)
                    result['reply_queued'] = True
            except Exception as error:
                self.context(token)
                delivery_id = (reply or report)['id']
                with self.c.db.transaction() as db:
                    queued = db.execute('SELECT id,state FROM outbox WHERE task=? AND payload_document=?',
                                        (ctx.task, delivery_id)).fetchone()
                stage = self.call(token, 'stage_receipt', {})
                result.update(state='delivery_receipt_recovered' if queued else 'saved_delivery_pending',
                              failed_step='delivery', error=str(error), reply_queued=bool(queued),
                              stage=stage['stage'], commits_by_layer=stage['commits_by_layer'])
                if queued:
                    result['reply'] = {'outbox':queued['id'], 'state':queued['state'],
                                       'guidance':'已存在发送记录；沿此编号查实际回执，不能换编号重发。'}
                return result
            return result
        if method == 'record':
            key = args.get('id') or uuid.uuid4().hex
            from .navigation import is_navigation
            if is_navigation(key):
                raise Denied('导航只保存入口，请用link_context登记目标，不在导航另写当前数值。')
            text = args['text']
            if 'lookup' in args:
                if args['kind']!='context' or args.get('append'):
                    raise ValueError('Object lookup metadata belongs to its current Context only')
                from .lookup import compose
                text = compose(self.c,ctx,text,args['lookup'])
            if args.get('append') and (args['kind'] != 'event' or 'base' not in args):
                raise ValueError('Incremental append is only for an event with explicit base')
            self.c.register(ctx, key, args['kind'])
            if args['kind']=='context' and 'lookup' not in args:
                from .lookup import retain
                text=retain(self.c,ctx,key,args.get('base',0),text)
            if args.get('append'):
                receipt = self.c.append_event(ctx, key, args['base'], args['text'], args['commit_id'], args.get('take_over', False))
            else:
                fence = self.c.lease(ctx, key, take_over=args.get('take_over', False))
                receipt = self.c.commit(ctx, key, args.get('base', 0), text, args['commit_id'], fence)
            return {k: receipt[k] for k in ('id', 'document', 'revision', 'state')}
        if method == 'append_data':
            receipt = self.c.append_data(ctx, args['event'], args['text'], args['commit_id'], args.get('take_over', False))
            return {k: receipt[k] for k in ('id', 'document', 'revision', 'state')}
        if method == 'reply':
            from .recording import deliver
            return deliver(self.c, ctx, args['text'], args['id'], args.get('recording'), args.get('finish', False))
        if method == 'skill_read':
            return self.skill_read(ctx, args['skill'])
        if method == 'adopt_skill':
            Skills(self.c).adopt(ctx, args['skill'], args['version'])
            return {'adopted': True}
        if method == 'propose':
            result = Governance(self.c).propose(ctx, args['body'], args.get('rule', 'unanimous'))
            from .platform_decisions import present
            result['presentation_outbox'] = present(self.c, ctx, result['id'], 'proposal-view-' + result['id'])
            result['confirmation'] = '请用飞书回复已展示的提案消息：同意／不同意。排队不代表送达。'
            return result
        raise Denied('Operation not exposed to workers')


UPDATE_FIELDS = {
    'record': (('id', 'kind', 'text', 'commit_id'), {'base', 'take_over', 'append', 'lookup'}),
    'append_data': (('event', 'text', 'commit_id'), {'take_over'}),
    'case_passage': (('event', 'text', 'commit_id'), {'base', 'citations', 'take_over'}),
    'patch_context': (('id', 'commit_id'), {'base', 'edits', 'effects', 'take_over'}),
    'link_context': (('id', 'commit_id'), {'base', 'target', 'target_revision', 'event', 'take_over'}),
}
FIELD_ALIASES = {'event_id': 'event', 'record_id': 'id（record/patch_context/link_context）或 event（case_passage/append_data）',
                 'passage': 'text', 'commit': 'commit_id', 'id': 'event（case_passage/append_data 用 event，不用 id）'}


def update_shape_problem(method_name, body):
    """Say exactly which field is wrong, before anything is written. Never relaxes a rule."""
    required, optional = UPDATE_FIELDS[method_name]
    problems = []
    missing = [k for k in required if not isinstance(body.get(k), str) or not body[k]]
    if missing:
        problems.append('缺少或不是非空字符串：' + '、'.join(missing) + '。%s 必填 %s。' % (method_name, ' / '.join(required)))
    unknown = sorted(set(body) - set(required) - optional)
    if unknown:
        problems.append('多出不属于该方法的字段：' + '、'.join(
            k + ('（应写作 ' + FIELD_ALIASES[k] + '）' if k in FIELD_ALIASES else '') for k in unknown) + '。')
    if method_name == 'case_passage':
        if type(body.get('base')) is not int or body.get('base') < 0:
            problems.append('base 必须是整数：新事项写 0，已有事项写它当前版本号。')
        event = body.get('event')
        if isinstance(event, str) and event.startswith('message-'):
            problems.append('event 是你为本事项自选的 ASCII 编号（如 event-quote-1），不是原消息 message-… 编号；原消息请放进 citations。')
        if not isinstance(body.get('citations', []), list):
            problems.append('citations 必须是数组，每项 {id, revision, quote}。')
    if method_name == 'record' and body.get('append') and type(body.get('base')) is not int:
        problems.append('append=true 时必须给整数 base（该事项当前版本号），text 只写新增段落。')
    return ' '.join(problems)


def serve(api, port):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *args):
            pass  # Never print tokens, business bodies or raw exception paths.

        def answer(self, status, body):
            raw = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            # Stateless streamable HTTP: no server-to-client event stream.
            self.answer(405, {'error': 'POST required'})

        def do_POST(self):
            try:
                if self.headers.get('Origin') or self.headers.get('Host') != f'127.0.0.1:{self.server.server_port}':
                    raise Denied('Local host binding required')
                length = int(self.headers.get('Content-Length', '-1'))
                if not 0 <= length <= 8 * 1024 * 1024:
                    raise ValueError('Invalid body length')
                self.connection.settimeout(15)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('Incomplete body')
                if self.path == '/ingress':
                    expected = hmac.new(api.transport_secret, raw, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(expected, self.headers.get('X-LOOM-Signature', '')):
                        raise Denied('Transport authentication failed')
                    result = api.accept(json.loads(raw))
                elif self.path in ('/rpc', '/mcp'):
                    authorization = self.headers.get('Authorization', '')
                    if not authorization.startswith('Bearer '):
                        raise Denied('Worker authentication required')
                    request = json.loads(raw)
                    token = authorization[7:]
                    if self.path == '/mcp':
                        # Authenticate discovery and notifications too. Each
                        # business call still rechecks current task authority.
                        api.context(token)
                        if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
                            raise ValueError('Invalid MCP request')
                        result = handle_mcp(request, lambda m, a: api.call(token, m, a))
                        if result is None:
                            self.send_response(202)
                            self.send_header('Content-Length', '0')
                            self.end_headers()
                        else:
                            self.answer(200, result)
                        return
                    result = api.call(token, request['method'], request.get('args', {}))
                else:
                    raise Denied('Endpoint unavailable')
                self.answer(200, {'ok': True, 'result': result})
            except Conflict as error:
                self.answer(409, {'ok': False, 'conflict': error.detail})
            except Denied as error:
                self.answer(403, {'ok': False, 'error': str(error)})
            except (ValueError, KeyError, TypeError):
                self.answer(400, {'ok': False, 'error': 'Invalid request'})
            except Exception:
                self.answer(503, {'ok': False, 'error': 'Controller operation incomplete; inspect protected diagnostics'})
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server
