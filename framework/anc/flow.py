"""Conversation flow (落地日志 E297/E298): the ordered, ChatGPT-like thread of THIS chat, assembled by the program for every task.

It is the current-position context only; library lookup (search/read/case_catalog) stays a separate, on-demand path.
Entries are the chat's own messages (people's and the bot's saved replies) in the order the platform delivered them to us
(tasks.created), each marked from state that is computed on every call and never stored, so a mark can never go stale:

  【本次要处理】  the message this task was started for
  【新】          arrived after the last turn that finished successfully (the mark disappears by itself once a later turn is done)
  【未处理】      an earlier message whose turn failed / was interrupted / is still waiting
  【已取消】      the turn for this message was cancelled: still shown (nothing is lost), the model decides whether it still matters
  系统记录        real actions and results of earlier turns taken from the program's own records (records saved, reports saved, exports
                  requested, proposals, send errors), never from the model's own account of what it did
  (no mark)       earlier context

【新】 means "arrived since the last finished turn", not "must be answered": which sentences need an answer and whether the topic
continues stays the model's judgement.  Text shown here is an excerpt and a pointer, not a read: to adopt or cite something the
model still reads the original.  Only documents the whole current audience may read are shown (same check as recent_records),
and the marks are written by the program outside the people's text, whose own look-alike characters are neutralised."""
import json
from datetime import datetime, timedelta, timezone

MAX_ENTRIES = 30
EXCERPT = 400
BUDGET = 9000
UNHANDLED = ('needs_attention', 'interrupted', 'queued', 'preparing', 'running')
# Actions tied to a document must name one (detail key) that the whole current audience may read; actions whose audit detail names no
# checkable document (report.view_saved, report.export_request) are NOT shown, so a restricted record's existence is never hinted at.
DOCUMENT_KEY = {'report.saved': 'source', 'web.saved': 'document'}
ACTIONS = {'report.saved': '保存了一份报表',
           'web.saved': '保存了网页内容', 'proposal.created': '发起了一项确认提案', 'proposal.applied': '确认提案已生效',
           'reply.delivery_error': '答复发送出错', 'reply.unknown': '答复发送结果不明（不要重发）', 'delivery.not_sent': '答复未发送'}
TABLE = str.maketrans({'【': '〔', '】': '〕', '<': '‹', '>': '›'})


def moment(value):
    try:
        got = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def safe(text):
    return str(text).translate(TABLE)


def message_text(raw):
    try:
        envelope = json.loads(raw)
        content = json.loads(envelope.get('content') or '{}')
        text = content.get('text') or '[' + str(envelope.get('message_type') or '非文字消息') + ']'
        for mention in envelope.get('mentions') or []:                   # Feishu writes "@_user_1" in the text; the real name is in the mentions list
            if isinstance(mention, dict) and mention.get('key') and mention.get('name'):
                text = text.replace(str(mention['key']), '@' + str(mention['name']))
        return text
    except (ValueError, AttributeError, TypeError):
        return '[无法解析的消息]'


def reply_text(raw):
    try:
        got = json.loads(raw)
        if isinstance(got, dict):
            return str(got.get('text') or got.get('content') or raw)
    except ValueError:
        pass
    return raw


def build(c, ctx):
    """Return {'text': block, 'entries': n, 'new': n} for the chat of ctx, or None when there is nothing before this message."""
    with c.lock, c.db.transaction() as db:
        policy = c._check(db, ctx)
        current = db.execute('SELECT created FROM tasks WHERE id=?', (ctx.task,)).fetchone()
        if not current:
            return None
        from .conversation import bot
        app = bot(db, ctx.task, policy.state)
        cursor = next((r for r in db.execute("SELECT id,created FROM tasks WHERE scope=? AND kind='turn' AND state='done' AND cancelled=0 AND created<? AND event IS NOT NULL "
                            'ORDER BY created DESC', (ctx.scope, current['created']))
                            if bot(db, r['id'], policy.state) == app), None)
        rows = db.execute("SELECT t.id,t.actor,t.kind,t.state,t.cancelled,t.created,m.resource FROM tasks t JOIN messages m ON m.id=t.event "
                          "WHERE t.scope=? AND t.kind IN ('turn','receipt') AND t.created<=? "
                          'ORDER BY t.created DESC LIMIT ?', (ctx.scope, current['created'], MAX_ENTRIES)).fetchall()
        rows = [row for row in rows if bot(db, row['id'], policy.state) == app]
        if not rows:
            return None
        oldest = rows[-1]['created']
        names = {r[0]: (r[2] or r[1]) for r in db.execute('SELECT member,display_name,call_name FROM member_labels')}      # how they asked to be called, else the platform name
        entries, visible = [], []
        for row in reversed(rows):
            try:
                doc, resource = c._document(db, row['resource'])
            except Exception:
                continue
            if doc['revision'] <= 0 or doc['pending'] or not policy.can_read(ctx, resource):
                continue
            text = message_text(c._read(db, doc).decode('utf-8', 'replace'))
            if row['id'] == ctx.task:
                mark = '本次要处理'
            elif row['cancelled']:
                mark = '已取消'
            elif cursor is None or row['created'] > cursor['created']:
                mark = '新'
            elif row['kind'] == 'turn' and row['state'] in UNHANDLED:
                mark = '未处理'
            else:
                mark = ''
            if row['id'] != ctx.task:
                visible.append(row['id'])
            entries.append({'at': row['created'], 'who': names.get(row['actor']) or '成员', 'text': text, 'id': row['resource'], 'mark': mark})
        def readable(key):
            try:
                doc, resource = c._document(db, key)
            except Exception:
                return False
            return doc['revision'] > 0 and not doc['pending'] and policy.can_read(ctx, resource)

        if visible:                                                   # one query each, not one per turn: audit/commits have no index on task, so a per-turn lookup would scan the whole table 30 times
            marks = ','.join('?' * len(visible))
            for at, action, detail in db.execute("SELECT time,action,detail FROM audit WHERE action IN (%s) AND task IN (%s) ORDER BY id" % (','.join('?' * len(ACTIONS)), marks), (*ACTIONS, *visible)):
                key = DOCUMENT_KEY.get(action)
                if key:
                    try:
                        target = json.loads(detail).get(key)
                    except (ValueError, AttributeError):
                        target = None
                    if not isinstance(target, str) or not readable(target):
                        continue
                entries.append({'at': at, 'who': '系统记录', 'text': ACTIONS[action], 'id': '', 'mark': ''})
            per_task = {}
            for task, document, created in db.execute("SELECT c.task,c.document,max(c.created) FROM commits c JOIN documents d ON d.id=c.document "
                                                      "WHERE c.task IN (%s) AND c.state='committed' AND d.kind IN ('event','context','data') AND d.id NOT LIKE 'message-%%' "
                                                      'GROUP BY c.task,c.document' % marks, tuple(visible)):
                if readable(document):
                    per_task.setdefault(task, []).append(created)
            for task, stamps in per_task.items():
                entries.append({'at': max(stamps), 'who': '系统记录', 'text': '保存了 %d 份记录（记录/报表等文档）' % len(stamps), 'id': '', 'mark': ''})
        for out in db.execute('SELECT * FROM outbox WHERE scope=? AND created>=? AND created<=? ORDER BY created', (ctx.scope, oldest, current['created'])):
            if bot(db, out['task'], policy.state) != app:
                continue
            try:
                doc, resource = c._document(db, out['payload_document'])
            except Exception:
                continue
            if doc['revision'] <= 0 or doc['pending'] or not policy.can_read(ctx, resource):
                continue
            entries.append({'at': out['created'], 'who': '你(助手)', 'text': reply_text(c._read(db, doc).decode('utf-8', 'replace')), 'id': out['payload_document'],
                            'mark': '', 'state': '已送达' if out['state'] == 'sent' else '已保存、送达未确认'})
    entries.sort(key=lambda e: moment(e['at']) or datetime.min.replace(tzinfo=timezone.utc))
    members = [names.get(p) or '成员' for p in (policy.state['scopes'].get(ctx.scope, {}).get('participants') or [])]
    here = moment(current['created']) or datetime.now(timezone.utc)
    zone = timezone(timedelta(hours=8))
    lines, used = [], 0
    for entry in reversed(entries):                                   # newest first, so the budget cuts the oldest
        cut = len(entry['text']) > EXCERPT
        shown = safe(entry['text'][:EXCERPT]) + ('…（已截断）' if cut else '')
        at = moment(entry['at'])
        stamp = at.astimezone(zone).strftime('%H:%M' if at.astimezone(zone).date() == here.astimezone(zone).date() else '%m-%d %H:%M') if at else '?'
        line = (('【' + entry['mark'] + '】') if entry['mark'] else '') + stamp + ' ' + safe(entry['who']) + (('（' + entry['state'] + '）') if entry.get('state') else '') + '：' + shown
        if cut or entry['mark']:
            line += ' ［原件 ' + entry['id'] + '］'
        if used + len(line) > BUDGET and lines:
            break
        used += len(line)
        lines.append(line)
    if not lines:
        return None
    lines.reverse()
    roster = ('\n本会话成员：' + '、'.join(safe(m) for m in members) + '（只列称呼，不含他们各自的资料）。') if members else ''
    text = (roster + '\n以下是本会话（私聊/群）按收到顺序排好的对话流，只含你和本聊天对象；带【】的标记由系统现算：【本次要处理】是本次任务对应的消息，'
            '【新】是自上次成功完成的回合以来到达的消息（不等于每条都要答复，哪几句需要答复、话题是否延续由你判断），【未处理】是此前失败或未完成的回合，【已取消】是已被取消的回合（仍列出，是否还要紧由你判断）。“系统记录”是程序记下的真实动作，以它为准，不以任何自述为准。'
            '这里只是摘录和入口，不算你已读；要采用或引用某条，仍须用 anc.read 读原件。需要别的资料（知识库、旧事项）另用 search/case_catalog 查，不在此列。'
            '更早的消息未列出，需要时再查。\n<<<对话流开始>>>\n' + '\n'.join(lines) + '\n<<<对话流结束>>>\n')
    return {'text': text, 'entries': len(lines), 'new': sum(1 for line in lines if line.startswith('【新】') or line.startswith('【本次要处理】'))}
