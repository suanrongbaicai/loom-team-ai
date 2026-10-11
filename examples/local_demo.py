"""Offline walkthrough using Loom's real control API and synthetic decisions.

No platform SDK, model subscription, credential or network request is required.
The fixed model text and local receipts are deliberately labelled mock.
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'framework'))
from anc.control import Control
from anc.policy import Denied
from tools.policy_open import switch


class MockModel:
    def profile(self, original):
        if '活动安排' not in original:
            raise ValueError('The demo supports its fixed synthetic scenario only')
        return '成员 A：负责活动安排。来源：demo-source；类型：本人陈述。'


class MockTransport:
    def __init__(self):
        self.deliveries = []

    def send(self, payload):
        self.deliveries.append(payload['text'])
        return 'mock-local-receipt-' + str(len(self.deliveries))


def run(root):
    root = Path(root).absolute()
    if any(root.iterdir()):
        raise ValueError('Use an empty directory; the demo never overwrites existing data')
    control = Control(root / 'control')
    policy = {'epoch': 0, 'team_open': False, 'grants': [],
              'members': {who: {'active': True, 'verified': True, 'roles': [],
                                'bindings': []} for who in ('demo-a', 'demo-b')},
              'scopes': {who + '-private': {'kind': 'private', 'participants': [who],
                        'membership_verified': True, 'root': str(root / who)}
                        for who in ('demo-a', 'demo-b')}}
    control.set_policy(policy, 'synthetic-demo-initialization')
    ctx = control.task('demo-a', 'demo-a-private')
    trace = []
    original = '我是成员 A，负责活动安排。请记下来，后续安排活动可以先找我。'
    raw = control.append_data(ctx, 'demo-source', original, 'demo-original-v1')
    trace.append({'step': 'Data', 'result': '原件已保存', 'text': original})
    control.register(ctx, 'demo-introduction', 'event')
    event_text = '成员 A 提供了活动安排职责。实际采用原件 demo-source，原件版本 1。'
    control.commit(ctx, 'demo-introduction', 0, event_text,
                   'demo-event-v1', control.lease(ctx, 'demo-introduction'))
    control.register(ctx, 'demo-person-a', 'context')
    profile = MockModel().profile(original)
    control.commit(ctx, 'demo-person-a', 0, profile,
                   'demo-context-v1', control.lease(ctx, 'demo-person-a'))
    trace += [{'step': 'CL', 'result': '事项经过已保存', 'text': event_text},
              {'step': 'Context', 'result': '当前认识已保存', 'text': profile}]
    transport = MockTransport()
    saved = control.save_reply(ctx, '已保存这条合成职责记录。', 'demo-reply')
    oid = control.queue_reply(ctx, saved['id'], finish=True)
    payload = control.begin_send(oid)
    receipt = transport.send(payload)
    control.sent(oid, receipt)
    trace.append({'step': 'Delivery', 'result': 'sent（本地 mock）', 'receipt': receipt})

    resumed = control.task('demo-a', 'demo-a-private')
    found = control.read(resumed, 'demo-person-a')
    trace.append({'step': 'Resume', 'result': '新任务读到已有档案', 'revision': found['revision'], 'text': found['text']})
    control.cancel(resumed)

    def check_other(label, should_read):
        other = control.task('demo-b', 'demo-b-private')
        try:
            read = control.read(other, 'demo-person-a')
            allowed = True
        except Denied:
            allowed = False
        finally:
            control.cancel(other)
        if allowed != should_read:
            raise AssertionError('Unexpected disclosure decision')
        trace.append({'step': label, 'result': '允许读取' if allowed else '拒绝读取'})

    check_other('Sharing off', False)
    switch(control, 'synthetic-only-demo-sharing-approval', on=True, apply=True)
    check_other('Sharing on', True)
    switch(control, 'synthetic-only-demo-sharing-revocation', on=False, apply=True)
    check_other('Sharing off again', False)

    uncertain = control.task('demo-a', 'demo-a-private')
    saved_unknown = control.save_reply(uncertain, '这是一条模拟发送状态不明的回复。', 'demo-unknown')
    unknown_id = control.queue_reply(uncertain, saved_unknown['id'], finish=True)
    control.begin_send(unknown_id)
    control.sent(unknown_id, None)
    try:
        control.begin_send(unknown_id)
    except Denied:
        trace.append({'step': 'Unknown delivery', 'result': 'unknown；重复发送被拒绝'})
    else:
        raise AssertionError('Unknown delivery must not be replayed')

    current = control.task('demo-a', 'demo-a-private')
    assert control.read(current, raw['document'])['text'].startswith(original)
    control.cancel(current)
    result = {'mode': 'offline-mock', 'network_requests': 0, 'model_requests': 0,
              'platform_requests': 0, 'team_open_final': False,
              'mock_deliveries': len(transport.deliveries), 'trace': trace}
    (root / 'demo-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Empty output directory; omit to use a cleaned temporary directory')
    args = parser.parse_args()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        result = run(args.output)
    else:
        with tempfile.TemporaryDirectory(prefix='anc-demo-', dir=Path.cwd()) as path:
            result = run(Path(path))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
