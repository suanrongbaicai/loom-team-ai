import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from loom.control import Conflict, Control, RecoveryRequired
from loom.policy import Denied, StaleContext


def fixture(root):
    return {
        'epoch': 0,
        'members': {p: {'active': True, 'verified': True, 'roles': ['manager'] if p == 'bob' else ['member'],
                        'bindings': [{'platform': 'feishu', 'tenant': 'team', 'app': 'app-' + p, 'account': p, 'evidence': 'fixture-only'}]}
                    for p in ('alice', 'bob', 'carol')},
        'scopes': {name: {'kind': kind, 'participants': people, 'membership_verified': True, 'root': str(root / name)}
                   for name, kind, people in [('alice-private', 'private', ['alice']), ('bob-private', 'private', ['bob']),
                                             ('group', 'group', ['alice', 'bob']), ('mixed', 'group', ['alice', 'bob', 'carol'])]},
        'grants': [],
    }


def grant(mode='on_demand', **changes):
    value = {'id': 'grant-1', 'mode': mode, 'active': True, 'approval': 'test-owner-consent',
             'starts_at': '2020-01-01T00:00:00+00:00', 'expires_at': None,
             'owners': ['alice'], 'source_scopes': ['alice-private'], 'kinds': ['context'],
             'destinations': ['group', 'mixed', 'bob-private'], 'resources': ['customer'],
             'recipients': ['bob'], 'roles': [], 'include_history': True, 'include_future': True}
    value.update(changes)
    return value


class ControlTests(unittest.TestCase):
    def setUp(self):
        local = Path('.test-work')
        local.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=local)
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.state = fixture(self.root)
        self.c.set_policy(self.state, 'synthetic-test')
        self.a = self.c.task('alice', 'alice-private')

    def tearDown(self):
        self.temp.cleanup()

    def record(self, ctx, key='customer', text='客户当前人数 8', kind='context', base=0, fence=None):
        self.c.register(ctx, key, kind)
        fence = fence or self.c.lease(ctx, key)
        return self.c.commit(ctx, key, base, text, uuid.uuid4().hex, fence)

    def allow(self, item):
        self.state['grants'] = [item]
        self.c.set_policy(self.state, 'synthetic-grant')

    def test_a_host_read_is_rechecked_but_never_counts_as_the_model_reading(self):
        from loom.references import excerpt_was_read, read_versions
        a = self.c.task('alice', 'alice-private')
        self.record(a, 'note', '只有程序读过的正文', kind='data')
        self.assertEqual(self.c.host_read(a, 'note')['text'], '只有程序读过的正文')
        with self.c.db.transaction() as db:
            self.assertTrue(db.execute('SELECT 1 FROM task_reads WHERE task=? AND document=?', (a.task, 'note')).fetchone())
            self.assertFalse(excerpt_was_read(db, a.task, 'note', 1, 0, 3))
            self.assertEqual(read_versions(db, a.task, 'note'), [])
        self.c.read(a, 'note')
        with self.c.db.transaction() as db:
            self.assertTrue(excerpt_was_read(db, a.task, 'note', 1, 0, 3))

    def test_on_demand_private_record_is_usable_in_group(self):
        self.record(self.a)
        g = self.c.task('bob', 'group')
        with self.assertRaises(Denied):
            self.c.read(g, 'customer')
        self.allow(grant())
        g = self.c.task('bob', 'group')
        self.assertIn('人数 8', self.c.read(g, 'customer')['text'])
        self.assertEqual(len(self.c.search(g, '客户')['results']), 1)

    def test_continuous_all_work_covers_new_records_without_new_approval(self):
        self.record(self.a)
        self.allow(grant('all_work'))
        a = self.c.task('alice', 'alice-private')
        self.record(a, key='future', text='后续新工作资料')
        g = self.c.task('bob', 'group')
        self.assertEqual(self.c.read(g, 'future')['text'], '后续新工作资料')
        self.assertEqual(self.c.read(g, 'future')['text'], '后续新工作资料')
        with self.c.db.transaction() as db:
            self.assertEqual(len(self.c.db.get_policy(db)['grants']), 1)

    def test_hierarchy_positive_but_partial_audience_does_not_receive(self):
        self.record(self.a)
        self.allow(grant('hierarchy', recipients=[], roles=['manager']))
        manager_private = self.c.task('bob', 'bob-private')
        group = self.c.task('bob', 'group')
        mixed = self.c.task('bob', 'mixed')
        self.assertIn('人数', self.c.read(manager_private, 'customer')['text'])
        self.assertIn('人数', self.c.read(group, 'customer')['text'])
        with self.assertRaises(Denied):
            self.c.read(mixed, 'customer')
        self.assertEqual(self.c.search(mixed, '客户')['results'], [])

    def test_scope_grant_does_not_open_other_documents_or_kinds(self):
        self.record(self.a)
        self.record(self.a, key='secret', text='私有资料')
        self.allow(grant())
        g = self.c.task('bob', 'group')
        self.assertIn('人数', self.c.read(g, 'customer')['text'])
        with self.assertRaises(Denied):
            self.c.read(g, 'secret')
        with self.assertRaises(Denied):
            self.c.lease(g, 'customer')

    def test_expiring_loaded_grant_stops_saved_reply_delivery(self):
        self.record(self.a)
        self.allow(grant(expires_at='2099-01-01T00:00:00+00:00'))
        g = self.c.task('bob', 'group')
        self.c.search(g, '客户')
        saved = self.c.save_reply(g, '客户当前人数 8')
        oid = self.c.queue_reply(g, saved['id'])
        from datetime import datetime, timezone
        with patch('loom.policy.datetime') as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            clock.now.return_value = datetime(2099, 1, 2, tzinfo=timezone.utc)
            with self.assertRaises(Denied):
                self.c.begin_send(oid)
            with self.assertRaises(Denied):
                self.c.save_reply(g, '另存也不能绕过到期授权')

    def test_revoke_and_audience_change_invalidate_loaded_context(self):
        self.record(self.a)
        self.allow(grant('all_work'))
        g = self.c.task('bob', 'group')
        self.c.read(g, 'customer')
        self.state['scopes']['group']['participants'].append('carol')
        self.c.set_policy(self.state, 'member-added')
        with self.assertRaises(StaleContext):
            self.c.read(g, 'customer')
        with self.assertRaises(Denied):
            self.c.read(self.c.task('bob', 'group'), 'customer')

    def test_stage_file_name_is_short_even_with_maximum_length_identifiers(self):
        key='d'*96
        ctx=self.a
        self.c.register(ctx,key,'context')
        seen=[]
        self.c.failpoint=lambda where: seen.append(where)
        receipt=self.c.commit(ctx,key,0,'长标识也能写入',('c'*96),self.c.lease(ctx,key))
        self.assertEqual(receipt['state'],'committed')
        with self.c.db.transaction() as db:
            stage=Path(db.execute("SELECT stage FROM commits WHERE id=?",('c'*96,)).fetchone()[0])
        self.assertLessEqual(len(stage.name),45)                     # old name was 1+99+1+96+8 = 205 characters
        self.assertNotIn(key,stage.name); self.assertNotIn('c'*96,stage.name)

    def test_saved_original_cannot_be_rewritten(self):
        self.record(self.a, kind='data')
        with self.assertRaises(Denied):
            self.record(self.a, text='changed', kind='data', base=1)

    def test_event_data_append_keeps_original_and_retry_does_not_duplicate(self):
        first = self.c.append_data(self.a, 'northshore', '原话：两人明天离岗\n', 'append1')
        second = self.c.append_data(self.a, 'northshore', '更正：下周一离岗\n', 'append2')
        self.assertEqual(first['document'], second['document'])
        self.assertEqual(self.c.append_data(self.a, 'northshore', '原话：两人明天离岗\n', 'append1')['revision'], 1)
        self.assertEqual(self.c.append_data(self.a, 'northshore', '更正：下周一离岗\n', 'append2')['revision'], 2)
        with self.assertRaises(Denied):
            self.c.append_data(self.a, 'northshore', '改写旧原话\n', 'append1')
        with self.assertRaises(Denied):
            self.c.commit(self.a, first['document'], 2, '删除原话', 'replace1', self.c.lease(self.a, first['document']))
        self.assertEqual((self.root/'alice-private/Data/events/northshore.md').read_text(encoding='utf-8'),
                         '原话：两人明天离岗\n更正：下周一离岗\n')

    def test_event_append_recovers_exactly_once_after_file_replacement(self):
        self.c.append_data(self.a, 'case', '第一条\n', 'append-first')
        def crash(point):
            if point == 'file_replaced':
                raise RuntimeError('simulated crash')
        self.c.failpoint = crash
        with self.assertRaises(RuntimeError):
            self.c.append_data(self.a, 'case', '第二条\n', 'append-next')
        self.c.failpoint = lambda _: None
        self.c.recover()
        self.c.append_data(self.a, 'case', '第二条\n', 'append-next')
        path = self.root/'alice-private/Data/events/case.md'
        self.assertEqual(path.read_text(encoding='utf-8'), '第一条\n第二条\n')
        other = self.c.task('alice', 'alice-private')
        with self.assertRaises(Denied):
            self.c.append_data(other, 'case', '第三条\n', 'append-third', take_over=True)
        self.c.cancel(self.a)
        self.c.append_data(other, 'case', '第三条\n', 'append-third')
        self.assertEqual(path.read_text(encoding='utf-8'), '第一条\n第二条\n第三条\n')

    def test_new_group_member_does_not_silently_inherit_group_history(self):
        g = self.c.task('alice', 'group')
        self.record(g, key='group-history')
        self.state['scopes']['group']['participants'].append('carol')
        self.c.set_policy(self.state, 'member-added-not-history-consent')
        with self.assertRaises(Denied):
            self.c.read(self.c.task('bob', 'group'), 'group-history')
        self.state['grants'] = [grant(resources=['group-history'], source_scopes=['group'],
                                       recipients=['carol'], destinations=['group'])]
        self.c.set_policy(self.state, 'explicit-history-consent')
        self.assertIn('人数', self.c.read(self.c.task('bob', 'group'), 'group-history')['text'])

    def test_same_content_revision_still_preserves_conflict_base(self):
        self.record(self.a, text='unchanged')
        self.record(self.a, base=1, text='unchanged')
        with self.assertRaises(Conflict) as error:
            self.record(self.a, base=1, text='new proposal')
        self.assertEqual(error.exception.detail['base_text'], 'unchanged')

    def test_conflict_returns_original_current_and_proposed(self):
        self.record(self.a, text='人数 8')
        self.record(self.a, base=1, text='人数 9')
        with self.assertRaises(Conflict) as error:
            self.record(self.a, base=1, text='人数 10')
        detail = error.exception.detail
        self.assertEqual(detail['base_text'], '人数 8')
        self.assertEqual(detail['current_text'], '人数 9')
        self.assertEqual(detail['proposed_text'], '人数 10')
        self.assertEqual(self.c.read(self.a, 'customer')['text'], '人数 9')

    def test_crash_recovery_at_each_file_state_boundary(self):
        for point in ('stage_written', 'journal_prepared', 'file_replaced', 'before_state_commit', 'state_committed'):
            with self.subTest(point=point):
                key = point
                def fail(at):
                    if at == point:
                        raise RuntimeError('simulated crash')
                self.c.failpoint = fail
                with self.assertRaises(RuntimeError):
                    self.record(self.a, key=key)
                self.c.failpoint = lambda _: None
                self.c.recover()
                result = self.c.read(self.a, key)
                self.assertEqual(result['revision'], 0 if point == 'stage_written' else 1)
                self.assertEqual(result['text'], '' if point == 'stage_written' else '客户当前人数 8')

    def test_unjournaled_stage_cannot_be_replaced_with_changed_bytes(self):
        def fail(at):
            if at == 'stage_written':
                raise RuntimeError('exit before journal')
        self.c.failpoint = fail
        with self.assertRaises(RuntimeError):
            self.c.append_data(self.a, 'same-event', 'first bytes', 'stable-attempt')
        self.c.failpoint = lambda _: None
        with self.assertRaises(RecoveryRequired):
            self.c.append_data(self.a, 'same-event', 'other bytes', 'stable-attempt')
        completed = self.c.append_data(self.a, 'same-event', 'first bytes', 'stable-attempt')
        self.assertEqual(self.c.read(self.a, completed['document'])['text'], 'first bytes')

    def test_unjournaled_link_is_rejected_without_changing_source(self):
        import os
        self.c.register(self.a, 'linked', 'context')
        with self.c.db.transaction() as db:
            row, _ = self.c._document(db, 'linked')
            path = self.c._path(db, row)
        path.parent.mkdir(parents=True, exist_ok=True)
        source = self.root / 'preserved.txt'
        source.write_bytes(b'unchanged')
        from loom.control import digest
        stage = path.with_name('.' + digest(b'link-attempt')[:32] + '.pending')   # the stage name is derived from the commit id
        os.link(source, stage)
        with self.assertRaises(RecoveryRequired):
            self.c.commit(self.a, 'linked', 0, 'unchanged', 'link-attempt', self.c.lease(self.a, 'linked'))
        self.assertEqual(source.read_bytes(), b'unchanged')

    def test_send_result_and_outbox_are_committed_together(self):
        receipt = self.c.save_reply(self.a, '已完成并保存')
        oid = self.c.queue_reply(self.a, receipt['id'], finish=True)
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM tasks WHERE id=?', (self.a.task,)).fetchone()[0], 'done')
            self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?', (oid,)).fetchone()[0], 'queued')
        self.assertEqual(self.c.begin_send(oid)['text'], '已完成并保存')
        self.c.sent(oid)  # Platform may have accepted it, but response was lost.
        with self.assertRaises(Denied):
            self.c.begin_send(oid)
        self.c.sent(oid, 'platform-message-1')

    def test_final_reply_retry_is_idempotent_and_payload_cannot_change(self):
        receipt = self.c.save_reply(self.a, '已保存', 'stable-reply')
        oid = self.c.queue_reply(self.a, receipt['id'], finish=True)
        repeated = self.c.save_reply(self.a, '已保存', 'stable-reply')
        self.assertEqual(receipt['id'], repeated['id'])
        self.assertEqual(oid, self.c.queue_reply(self.a, repeated['id'], finish=True))
        with self.assertRaises(Denied):
            self.c.save_reply(self.a, '改写已保存结果', 'stable-reply')
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)

    def test_cancel_before_send_and_after_network_attempt(self):
        receipt = self.c.save_reply(self.a, '进度')
        oid = self.c.queue_reply(self.a, receipt['id'])
        self.assertFalse(self.c.cancel(self.a)['external_attempts_may_have_completed'])
        with self.assertRaises(Denied):
            self.c.begin_send(oid)
        second = self.c.task('alice', 'alice-private')
        receipt = self.c.save_reply(second, '另一条进度')
        oid = self.c.queue_reply(second, receipt['id'])
        self.c.begin_send(oid)
        self.assertTrue(self.c.cancel(second)['external_attempts_may_have_completed'])
        self.c.sent(oid, 'already-accepted')

    def test_no_cross_task_saved_reply_receipt(self):
        saved = self.c.save_reply(self.a, 'private reply')
        other = self.c.task('bob', 'group')
        with self.assertRaises(Denied):
            self.c.queue_reply(other, saved['id'])

    def test_revoked_context_can_still_cancel_only_its_bound_task(self):
        receipt = self.c.save_reply(self.a, '尚未发送')
        oid = self.c.queue_reply(self.a, receipt['id'])
        self.c.set_policy(self.state, 'revocation')
        self.assertTrue(self.c.cancel(self.a)['cancelled'])
        self.assertTrue(self.c.cancel(self.a)['cancelled'])
        with self.c.db.transaction() as db:
            self.assertEqual(db.execute('SELECT state FROM outbox WHERE id=?', (oid,)).fetchone()[0], 'cancelled')
        from loom.policy import Context
        with self.assertRaises(Denied):
            self.c.cancel(Context('bob', self.a.scope, self.a.epoch, self.a.task))

    def test_active_writer_not_replaced_by_silence(self):
        self.record(self.a)
        newer = self.c.task('alice', 'alice-private')
        with self.assertRaises(Denied):
            self.c.lease(newer, 'customer', take_over=True)
        with self.c.db.transaction() as db:
            db.execute("UPDATE tasks SET state='interrupted' WHERE id=?", (self.a.task,))
        with self.assertRaisesRegex(Denied, 'document customer; explicit take_over=true'):
            self.c.lease(newer, 'customer')
        fence = self.c.lease(newer, 'customer', take_over=True)
        self.assertEqual(fence, 2)
        with self.assertRaises(Denied):
            self.record(self.a, base=1, text='old writer', fence=1)
        self.record(newer, key='different', text='new task')

    def test_attention_writer_requires_confirmed_end_and_explicit_takeover(self):
        self.record(self.a)
        newer = self.c.task('alice', 'alice-private')
        with self.c.db.transaction() as db:
            db.execute("UPDATE tasks SET state='needs_attention' WHERE id=?", (self.a.task,))
        with self.assertRaisesRegex(Denied, 'termination is unconfirmed'):
            self.c.lease(newer, 'customer', take_over=True)
        with self.c.db.transaction() as db:
            db.execute('INSERT INTO executions VALUES(?,?,?,?,?,?,?)',
                       (self.a.task,'fixture-job','fixture-output','running','fixture',None,None))
        with self.assertRaisesRegex(Denied, 'termination is unconfirmed'):
            self.c.lease(newer, 'customer', take_over=True)
        with self.c.db.transaction() as db:
            db.execute("UPDATE executions SET state='finished' WHERE task=?",(self.a.task,))
        with self.assertRaisesRegex(Denied, 'explicit take_over'):
            self.c.lease(newer, 'customer')
        self.assertEqual(self.c.lease(newer,'customer',take_over=True),2)
        with self.assertRaises(Denied): self.record(self.a,base=1,text='stale',fence=1)

    def test_authorized_group_writer_can_continue_shared_event_after_release(self):
        first = self.c.task('alice', 'group')
        self.record(first, key='shared-event', kind='event', text='共同事项开始')
        second = self.c.task('bob', 'group')
        with self.assertRaises(Denied):
            self.record(second, key='shared-event', kind='event', base=1, text='尚不可接管')
        self.c.cancel(first)
        self.record(second, key='shared-event', kind='event', base=1, text='共同事项开始；后续动作已完成')
        self.assertEqual(self.c.read(second, 'shared-event')['revision'], 2)
        with self.assertRaises(Denied):
            self.record(self.c.task('bob', 'bob-private'), key='shared-event', kind='event', base=2, text='跨范围覆盖')

    def test_finished_or_cancelled_writer_allows_next_turn_correction(self):
        for ending in ('done', 'cancelled', 'legacy_done'):
            with self.subTest(ending=ending):
                actor = self.c.task('alice', 'alice-private')
                key = 'correction-' + ending
                self.record(actor, key=key, text='两人周四离岗')
                if ending == 'done':
                    reply = self.c.save_reply(actor, '已记下')
                    self.c.queue_reply(actor, reply['id'], finish=True)
                elif ending == 'cancelled':
                    self.c.cancel(actor)
                else:
                    with self.c.db.transaction() as db:
                        db.execute("UPDATE tasks SET state='done' WHERE id=?", (actor.task,))
                if ending != 'legacy_done':
                    with self.c.db.transaction() as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM leases WHERE holder=? AND active=1', (actor.task,)).fetchone()[0], 0)
                newer = self.c.task('alice', 'alice-private')
                fence = self.c.lease(newer, key)
                self.assertEqual(fence, 2)
                self.record(newer, key=key, base=1, text='两人下周一离岗；依据本轮更正', fence=fence)
                self.assertEqual(self.c.read(newer, key)['revision'], 2)
                with self.assertRaises(Denied):
                    self.record(actor, key=key, base=2, text='旧写入者覆盖', fence=1)
                history = self.root / 'alice-private' / 'Context' / '.history' / (key + '.md') / '1.md'
                self.assertEqual(history.read_text(encoding='utf-8'), '两人周四离岗')

    def test_out_of_band_change_and_traversal_are_rejected(self):
        self.record(self.a)
        (self.root / 'alice-private/Context/customer.md').write_text('bypassed controller', encoding='utf-8')
        with self.assertRaises(RecoveryRequired):
            self.c.read(self.a, 'customer')
        with self.assertRaises(ValueError):
            self.c.register(self.a, '../escape', 'context')


if __name__ == '__main__':
    unittest.main()
