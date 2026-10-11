"""Model-selected navigation links, without another editable current-value copy."""
import os
import re
from urllib.parse import quote

from .control import identifier
from .policy import Denied
from .retrieval import metadata


def is_navigation(key):
    return key.startswith('context-index-')


def link(c, ctx, key, base, target, target_revision, commit_id, event=None, take_over=False):
    for value in (key, target, commit_id):
        identifier(value)
    if event is not None:
        identifier(event)
    if not is_navigation(key) or type(base) is not int or base < 0:
        raise ValueError('选择已登记的Context导航入口和明确版本。')
    if type(target_revision) is not int or target_revision < 1 or is_navigation(target):
        raise ValueError('导航指向正式对象Context的具体版本，不能再指向另一导航。')
    with c.lock:
        with c.db.transaction() as db:
            policy = c._check(db, ctx, True)
            obj, resource = c._document(db, target)
            policy.require_read(ctx, resource)
            if obj['kind'] != 'context':
                raise Denied('导航目标必须是正式Context。')
            body = c.version_bytes(db, obj, target_revision).decode('utf-8')
            info = metadata(body)
            if not info or not info.get('name'):
                raise Denied('先通过record.lookup保存有原件依据的对象名称，再加入导航。')
            case = None
            if event:
                case, resource = c._document(db, event)
                policy.require_read(ctx, resource)
                if case['kind'] != 'event' or case['revision'] < 1:
                    raise Denied('事项链接须指向已保存的CL。')
            # Names are supplied through the existing evidence-checked lookup
            # registration, never from free-form summary text at this interface.
            name = re.sub(r'[\r\n]+', ' ', info['name'])
            name = name.replace('\\', '\\\\').replace('[', '\\[').replace(']', '\\]')
        c.register(ctx, key, 'context')
        with c.db.transaction() as db:
            policy = c._check(db, ctx, True)
            nav, resource = c._document(db, key)
            policy.require_write(ctx, resource)
            before = c.version_bytes(db, nav, base).decode('utf-8') if base else ''
            directory = c._path(db, nav).parent
            def href(row):
                return quote(os.path.relpath(c._path(db, row), directory).replace('\\', '/'), safe='/.-_')
            line = '- [' + name + '](<' + href(obj) + '>)'
            if case:
                line += '；[事项档案](<' + href(case) + '>)'
            line += ' <!-- loom-navigation:' + target + ' -->'
            lines = before.splitlines(keepends=True)
            matches = [i for i, value in enumerate(lines)
                       if re.search(r'(?<![A-Za-z0-9_-])' + re.escape(target) + r'(?![A-Za-z0-9_-])', value)]
            if len(matches) > 1 or (matches and not lines[matches[0]].startswith('- ')):
                raise Denied('同一入口在旧导航有多处或非条目引用；保留原文，先由维护入口核对。')
            if matches:
                i = matches[0]
                lines[i] = line + ('\n' if lines[i].endswith('\n') else '')
                text = ''.join(lines)
            else:
                text = (before or '# 工作资料入口\n\n仅定位正式Context与事项，不另存当前数值。\n').rstrip('\n') + '\n' + line + '\n'
        fence = c.lease(ctx, key, take_over=take_over)
        receipt = c.commit(ctx, key, base, text, commit_id, fence)
        return {k: receipt[k] for k in ('id', 'document', 'revision', 'state')}
