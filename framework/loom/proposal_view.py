"""Exact approval content in a deterministic, readable Chinese presentation."""
from .policy import Denied

LABELS = {'policy_patch': '拟生效设置', 'grants': '内容共享', 'decision_rules': '批准规则',
          'handoff_rules': '交接规则', 'title': '事项', 'text': '具体内容', 'category': '适用事项类别',
          'id': '记录标识', 'mode': '共享方式', 'active': '启用', 'approval': '授权依据',
          'starts_at': '开始时间', 'expires_at': '失效时间', 'owners': '资料所属人',
          'source_scopes': '资料来源', 'kinds': '资料种类', 'destinations': '允许使用的会话',
          'resources': '指定资料', 'recipients': '获准人员', 'roles': '获准角色',
          'include_history': '包括已有历史', 'include_future': '包括后续新增',
          'approvers': '批准人', 'categories': '适用事项', 'policy_fields': '可以变更的设置',
          'validity_seconds': '确认有效秒数', 'initiators': '交接发起人', 'assignees': '接手人',
          'responsible': '责任人', 'scopes': '适用会话'}
VALUES = {'on_demand': '按指定资料共享', 'all_work': '范围内持续共享', 'hierarchy': '按获准角色共享',
          'data': '原始资料', 'event': '事件过程', 'context': '当前认识', 'reply': '回复',
          'pending': '等待确认', 'approved': '已批准', 'rejected': '未通过', 'stale_roster': '人员变化，需重新确认'}

def value_text(value, state, level=0):
    if level > 12:
        raise Denied('Proposal is too complex to display completely')
    if isinstance(value, dict):
        return '\n'.join('  '*level + LABELS.get(str(k), str(k)) + '：' +
                         ('\n' if isinstance(v, (list, dict)) and v else '') + value_text(v, state, level+1)
                         for k, v in value.items()) or '无'
    if isinstance(value, list):
        return '\n'.join('  '*level + '• ' + value_text(v, state, level+1) for v in value) or '无'
    if value is None:
        return '未设置'
    if isinstance(value, bool):
        return '是' if value else '否'
    raw = str(value)
    for section in ('members', 'scopes'):
        item = state.get(section, {}).get(raw)
        if item:
            return item.get('name') or item.get('nickname') or item.get('label') or raw
    return VALUES.get(raw, raw)

def render(view, state):
    status = '已自动生效' if view.get('applied_epoch') else VALUES.get(view['state'], view['state'])
    if not view['current'] and not view.get('applied_epoch'):
        status += '，当前已失效，不能确认'
    lines = ['待确认事项 · 第' + str(view['version']) + '版', '状态：' + status,
             '需要确认的人：' + '、'.join(value_text(p, state) for p in view['voters']),
             '确认截止：' + (view.get('expires_at') or '按当前已批准规则，未设置固定截止时间'),
             '具体内容：', value_text(view['body'], state)]
    if view['body'].get('policy_patch'):
        lines += ['以上设置将整体替换对应项目，未列出的原有条目会移除。',
                  '替换前的对应设置：', value_text(view.get('policy_base', {}), state)]
    if view['current'] and view['state'] == 'pending':
        lines += ['请使用飞书的“回复”功能回复本条消息，只写“同意”或“不同意”。',
                  '只确认本条展示的版本和范围；普通聊天中的“同意”不会算作表决。']
    text = '\n'.join(lines)
    if len(text.encode()) > 18000:
        raise Denied('Proposal exceeds readable message size; split into complete scoped proposals')
    return text
