"""Exact, model-selected Context edits; unchanged bytes are retained by the host."""
import json

from .control import digest, identifier
from .policy import Denied
from .references import excerpt_was_read


def patch(c, ctx, key, base, edits, commit_id, effects, take_over=False):
    identifier(key); identifier(commit_id)
    from .navigation import is_navigation
    if is_navigation(key):
        raise Denied('导航只保存入口，请用link_context登记目标，不在导航另写当前数值。')
    if type(base) is not int or base < 1:
        raise ValueError('已有Context需要明确已读版本。')
    if not isinstance(edits, list) or not 1 <= len(edits) <= 12:
        raise ValueError('一次提供1..12处明确替换。')
    if (not isinstance(effects, dict) or set(effects) != {'state', 'reason'}
            or effects['state'] not in ('updated', 'unchanged', 'pending')
            or not isinstance(effects['reason'], str) or not 1 <= len(effects['reason'].strip()) <= 2000):
        raise ValueError('说明派生结果已同步、未受影响或仍待核，并给出简短理由。')
    with c.lock:
        with c.db.transaction() as db:
            policy = c._check(db, ctx, True)
            row, resource = c._document(db, key)
            policy.require_write(ctx, resource)
            if row['kind'] != 'context':
                raise Denied('局部更新仅用于Context；原件和CL历史不走此接口。')
            before = c.version_bytes(db, row, base).decode('utf-8')
            header_end = (before.index('\n---\n', 4) + 5
                          if before.startswith('---\nloom_lookup: ') else 0)
            spans = []
            for edit in edits:
                if (not isinstance(edit, dict) or set(edit) != {'old', 'new'}
                        or not isinstance(edit['old'], str) or not edit['old']
                        or not isinstance(edit['new'], str) or edit['old'] == edit['new']):
                    raise ValueError('每处替换需old精确原文和不同的new；删除用空new。')
                if before.count(edit['old']) != 1:
                    raise Denied('原文须在指定版本唯一匹配；先读取足够定位的原文。')
                start = before.index(edit['old']); end = start + len(edit['old'])
                if start < header_end:
                    raise Denied('名称对应须通过record.lookup及其依据校验更新。')
                if not excerpt_was_read(db, ctx.task, key, base, start, end):
                    raise Denied('修改片段尚未在本轮实际读取。')
                spans.append((start, end, edit['new']))
            spans.sort()
            if any(a[1] > b[0] for a, b in zip(spans, spans[1:])):
                raise ValueError('替换范围重叠；请合并成一处明确修改。')
            text = before
            for start, end, new in reversed(spans):
                text = text[:start] + new + text[end:]
            from .retrieval import metadata
            if metadata(before) != metadata(text):
                raise Denied('名称对应须通过record.lookup及其依据校验更新。')
            if not text.strip() or len(text.encode('utf-8')) > 8 * 1024 * 1024:
                raise ValueError('Context须保留非空正文，最大8 MiB。')
            intent = {'commit': commit_id, 'document': key, 'base': base,
                      'edits': edits, 'effects': effects, 'digest': digest(text.encode())}
            previous = db.execute("SELECT detail FROM audit WHERE task=? AND action='context.patch_prepared' "
                                  "AND json_extract(detail,'$.commit')=? ORDER BY id DESC LIMIT 1",
                                  (ctx.task, commit_id)).fetchone()
            if previous and json.loads(previous['detail']) != intent:
                raise Denied('同次局部修改的内容或影响说明发生变化；核对后使用新的操作编号。')
            if not previous:
                c.audit(db, ctx.principal, ctx.task, 'context.patch_prepared', intent)
        fence = c.lease(ctx, key, take_over=take_over)
        receipt = c.commit(ctx, key, base, text, commit_id, fence)
        return {k: receipt[k] for k in ('id', 'document', 'revision', 'state')} | {
            'effects': effects,
            'coverage': '未修改的正文已保留；派生数值及条件正确性由模型核对，程序未作语义保证。'}
