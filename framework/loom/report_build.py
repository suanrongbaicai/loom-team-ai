"""Compose existing report stages without changing evidence, audience or send rules."""
from .control import digest, identifier
from .feishu_reports import CloudFailure
from .policy import Denied
from .web_reports import WebUnavailable


def build(leaf, cloud, ctx, request_id, spec, backup_format, guard, previous=None):
    """One model decision, fixed child IDs, existing durable stage receipts.

    A retry resumes the same operations. Native unknown stays unresolved;
    interactive recovery reads its deterministic remote ID before any write.
    No answer is sent and no semantic CL/Context decision is made here.
    """
    identifier(request_id)
    if backup_format not in ('docx', 'sheet'):
        raise ValueError('Choose native docx or sheet for the same-version backup')
    web = getattr(cloud, 'web', None)
    if web is None:
        raise Denied('交互报表发布尚未配置；请沿已有能力接续处理。')
    guard()
    saved = leaf.save(ctx, request_id, spec, previous)
    guard()
    out = {'operation': request_id, 'report': saved['report'],
           'version': saved['version'], 'checks': saved['checks']}
    native_id = 'cloud-' + digest((ctx.scope + ':' + request_id).encode())
    try:
        native = cloud.publish(ctx, request_id, saved['report'], backup_format, guard)
    except CloudFailure:
        # The existing publisher owns the durable error/unknown classification.
        # Inspect it; never create a replacement request to escape uncertainty.
        guard()
        native = cloud.status(ctx, native_id)
    guard()
    out['native'] = native
    if native['state'] != 'published':
        return dict(out, state='incomplete', next='核对本次飞书发布状态；保留相同id，不换编号重建。')
    try:
        interactive = web.publish(ctx, request_id, saved['report'], native['publication'], guard)
    except WebUnavailable:
        guard()
        return dict(out, state='incomplete',
                    interactive={'publication': 'web-' + digest((ctx.scope + ':' + request_id).encode()),
                                 'state': 'unconfirmed'},
                    next='交互版未核验；用相同参数和id接续，程序先回读已有发布结果。')
    guard()
    return dict(out, state='published', interactive=interactive, links=interactive['links'],
                next='核对结果，将本次实际采用和发布结果记入CL，再保存完整答复并发送链接。')
