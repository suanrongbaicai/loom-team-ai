import copy
import unittest

from anc.control import Conflict, Control, RecoveryRequired
from anc.policy import Denied
from tests import test_runtime


class ContextPatchTests(unittest.TestCase):
    setUp = test_runtime.RuntimeTests.setUp
    tearDown = test_runtime.RuntimeTests.tearDown
    install_rules = test_runtime.RuntimeTests.install_rules

    def ready(self, text='人数10；单价30元，含税；免配送。\n场地180元；总额480元。'):
        self.install_rules(); self.api.rules.load(self.ctx, 'SessionStart', 'patch-test')
        token = self.api.issue(self.ctx)
        self.api.call(token, 'record', {'id':'tea','kind':'context','text':text,'commit_id':'initial'})
        self.api.call(token, 'read', {'id':'tea'})
        return token

    def change(self, **kwargs):
        return dict(id='tea', base=1, commit_id='terms', edits=[
            {'old':'免配送。','new':'茶点满320元免配送，否则25元。'},
            {'old':'场地180元；总额480元。','new':'10人需场地220元；总额545元。'}],
            effects={'state':'updated','reason':'场地与配送变更同步重算总额，含税不变。'}, **kwargs)

    def test_conditions_replace_expired_terms_and_total_together(self):
        tok = self.ready(); result = self.api.call(tok, 'patch_context', self.change())
        self.assertEqual(result['revision'], 2)
        text = self.c.read(self.ctx, 'tea')['text']
        self.assertIn('单价30元，含税', text)
        self.assertIn('满320元免配送，否则25元', text)
        self.assertIn('总额545元', text); self.assertNotIn('总额480元', text)
        self.assertIn('总额480元', self.c.read(self.ctx, 'tea', revision=1)['text'])
        self.assertEqual(result, self.api.call(tok, 'patch_context', self.change()))

    def test_price_change_updates_dependent_total_and_preserves_tax(self):
        tok = self.ready()
        args = dict(id='tea', base=1, commit_id='price', edits=[
            {'old':'单价30元','new':'单价32元'}, {'old':'总额480元','new':'总额500元'}],
            effects={'state':'updated','reason':'10*32+180=500，税费/配送条件不变。'})
        self.api.call(tok, 'patch_context', args)
        text = self.c.read(self.ctx, 'tea')['text']
        self.assertIn('单价32元，含税；免配送', text); self.assertIn('总额500元', text)

    def test_program_does_not_claim_to_detect_stale_derived_value(self):
        tok = self.ready()
        args = dict(id='tea', base=1, commit_id='incomplete', edits=[{'old':'人数10','new':'人数12'}],
                    effects={'state':'pending','reason':'人数更正已记录，总额还需核算。'})
        result = self.api.call(tok, 'patch_context', args)
        self.assertEqual(result['effects']['state'], 'pending')
        self.assertIn('总额480元', self.c.read(self.ctx,'tea')['text'])
        # This is deliberately not a semantic pass. Real-model acceptance must
        # reject an answer which uses this stale total as the current total.

    def test_unread_range_overlap_and_changed_retry_are_rejected(self):
        tok = self.ready()
        cold = self.c.task('alice','group'); self.api.rules.load(cold,'SessionStart','cold-patch')
        ct = self.api.issue(cold)
        with self.assertRaises(Denied): self.api.call(ct, 'patch_context', self.change())
        args=self.change(); args['edits']=[{'old':'人数10','new':'人数12'},{'old':'人数10；','new':'人数8；'}]
        with self.assertRaises(ValueError): self.api.call(tok,'patch_context',args)
        self.api.call(tok,'patch_context',self.change())
        altered=self.change(); altered['effects']['reason']='不同说明'
        with self.assertRaises(Denied): self.api.call(tok,'patch_context',altered)

    def test_stale_base_preserves_newer_content_and_conflict_intent(self):
        tok=self.ready(); self.api.call(tok,'patch_context',self.change())
        args=self.change(); args['commit_id']='other'; args['edits']=[{'old':'人数10','new':'人数12'}]
        with self.assertRaises(Conflict): self.api.call(tok,'patch_context',args)
        self.assertEqual(self.c.read(self.ctx,'tea')['revision'],2)

    def test_originals_other_person_and_metadata_cannot_be_patched(self):
        tok=self.ready()
        self.api.call(tok,'record',{'id':'raw','kind':'data','text':'免配送。','commit_id':'raw-first'})
        args=self.change(); args['id']='raw'
        with self.assertRaises(Denied):self.api.call(tok,'patch_context',args)
        bob=self.c.task('bob','bob-private');self.c.register(bob,'secret','context')
        self.c.commit(bob,'secret',0,'免配送。','secret-first',self.c.lease(bob,'secret'))
        args['id']='secret'
        with self.assertRaises(Denied):self.api.call(tok,'patch_context',args)
        # Even an otherwise authorized body edit cannot introduce unchecked
        # object names/aliases through the metadata header.
        args=self.change();args['edits']=[{'old':'人数10',
            'new':'---\nanc_lookup: {"name":"别的事项","aliases":[]}\n---\n人数10'}]
        with self.assertRaises(Denied):self.api.call(tok,'patch_context',args)

    def test_same_patch_after_prepared_interruption_recovers_once(self):
        tok=self.ready()
        def stop(stage):
            if stage=='journal_prepared':raise RuntimeError('injected')
        self.c.failpoint=stop
        with self.assertRaises(RuntimeError):self.api.call(tok,'patch_context',self.change())
        self.c.failpoint=lambda _:None
        with self.assertRaises(RecoveryRequired):self.c.read(self.ctx,'tea')
        Control(self.c.root).recover()
        self.assertEqual(self.api.call(tok,'patch_context',self.change())['revision'],2)

    def test_patch_is_usable_in_work_batch(self):
        tok=self.ready()
        result=self.api.call(tok,'save_work',{'updates':[{'method':'patch_context','arguments':self.change()}]})
        self.assertEqual(result['state'],'saved')
        self.assertEqual(self.c.read(self.ctx,'tea')['revision'],2)
