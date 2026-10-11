from tests.fixtures.symlinks import symlink_or_skip
from pathlib import Path
import json
import os
import tempfile
import unittest

from loom.control import Control
from tests.test_control import fixture
from tools import purge_test_data as purge


class PurgeTests(unittest.TestCase):
    def setUp(self):
        Path('.test-work').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir='.test-work')
        self.root = Path(self.temp.name).absolute()
        self.c = Control(self.root / 'control')
        self.c.set_policy(fixture(self.root), 'test-policy')
        self.test_task = self.c.task('alice', 'alice-private')      # S: engineering synthetic
        self.real_task = self.c.task('alice', 'alice-private')      # K: real
        self.write(self.test_task, 'message-test-one', 'Synthetic test original', 'commit-test-one')
        self.write(self.test_task, 'event-test-one', 'Test event passage', 'commit-test-event')
        self.write(self.real_task, 'event-real-one', 'Real confirmation by a person', 'commit-real-one')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO messages VALUES('mid-test-1','feishu','t','chat','om_test','message-test-one')")
            db.execute("INSERT INTO messages VALUES('mid-real-1','feishu','t','chat','om_real',NULL)")
            for oid, task, state in (('out-test', self.test_task.task, 'delivered'), ('out-real', self.real_task.task, 'delivered'),
                                     ('out-unknown', self.real_task.task, 'unknown')):
                db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,NULL,'now')", (oid, task, 'alice-private', 1, 'event-real-one', 1, 'd', state))
            db.execute("INSERT INTO task_reads VALUES(?,?)", (self.test_task.task, 'event-real-one'))
            db.execute("INSERT INTO task_reads VALUES(?,?)", (self.real_task.task, 'event-real-one'))
        self.seed = {'tasks': [self.test_task.task], 'messages': ['mid-test-1'],
                     'protect': {'tasks': [self.real_task.task], 'documents': ['event-real-one'], 'outbox_unknown': ['out-unknown']}}
        self.never = lambda: False

    def tearDown(self):
        self.temp.cleanup()

    def write(self, ctx, key, text, commit):
        self.c.register(ctx, key, 'data')
        fence = self.c.lease(ctx, key)
        self.c.commit(ctx, key, 0, text, commit, fence)

    def rows(self, table):
        with self.c.db.transaction() as db:
            return [dict(r) for r in db.execute('SELECT * FROM %s' % table)]

    def archive_and_apply(self, seed=None, **kw):
        seed = seed or self.seed
        p = purge.plan(self.c, seed)
        purge.archive(p, self.root / 'archive', seed)
        return purge.apply(self.c, seed, self.root / 'archive', listening=self.never, **kw)

    def test_plan_lists_only_the_seed_closure_and_refuses_nothing(self):
        p = purge.plan(self.c, self.seed)
        self.assertEqual(p['refusals'], [])
        self.assertEqual({r['id'] for r in p['tables']['documents']}, {'message-test-one', 'event-test-one'})
        self.assertEqual({r['id'] for r in p['tables']['outbox']}, {'out-test'})
        self.assertEqual({r['id'] for r in p['tables']['tasks']}, {self.test_task.task})

    def test_apply_removes_only_the_selected_and_leaves_everything_else_identical(self):
        before_real = self.rows('documents')
        result = self.archive_and_apply()
        self.assertEqual(result['counts']['documents'], 2)
        self.assertEqual({r['id'] for r in self.rows('documents')}, {'event-real-one'})
        self.assertEqual({r['id'] for r in self.rows('outbox')}, {'out-real', 'out-unknown'})   # unknown-delivery row kept
        self.assertEqual({r['id'] for r in self.rows('tasks')}, {self.real_task.task})
        self.assertEqual([r for r in before_real if r['id'] == 'event-real-one'], [r for r in self.rows('documents')])
        self.assertEqual([r['task'] for r in self.rows('task_reads')], [self.real_task.task])
        tombstones = [r for r in self.rows('audit') if r['action'] == 'test_data.purged']
        self.assertEqual(len(tombstones), 1)
        self.assertNotIn('Synthetic test original', json.dumps(tombstones))                    # the tombstone carries no content
        self.assertTrue((self.root / 'archive/documents.jsonl').is_file())

    def test_files_are_removed_only_after_the_commit_and_are_archived_first(self):
        with self.c.db.transaction() as db:
            path = self.c._path(db, db.execute("SELECT * FROM documents WHERE id='message-test-one'").fetchone())
        self.assertTrue(path.exists())
        self.archive_and_apply()
        self.assertFalse(path.exists())
        archived = list((self.root / 'archive/files').glob('*message-test-one*'))
        self.assertTrue(archived)

    def test_rehearsal_on_a_copy_changes_rows_but_never_removes_files(self):
        with self.c.db.transaction() as db:
            path = self.c._path(db, db.execute("SELECT * FROM documents WHERE id='message-test-one'").fetchone())
        p = purge.plan(self.c, self.seed)
        purge.archive(p, self.root / 'archive', self.seed)
        purge.apply(self.c, self.seed, self.root / 'archive', listening=self.never, remove_files=False)
        self.assertTrue(path.exists())
        self.assertEqual({r['id'] for r in self.rows('documents')}, {'event-real-one'})

    def test_apply_refuses_while_the_controller_is_listening_or_without_a_matching_archive(self):
        p = purge.plan(self.c, self.seed)
        purge.archive(p, self.root / 'archive', self.seed)
        with self.assertRaisesRegex(purge.Refused, 'port is listening'):
            purge.apply(self.c, self.seed, self.root / 'archive', listening=lambda: True)
        changed = dict(self.seed, messages=[])                       # a different plan than the archived one
        with self.assertRaisesRegex(purge.Refused, 'does not match'):
            purge.apply(self.c, changed, self.root / 'archive', listening=self.never)
        self.assertEqual(len(self.rows('tasks')), 2)

    def test_protected_and_unknown_objects_block_the_plan(self):
        bad = json.loads(json.dumps(self.seed))
        bad['tasks'].append(self.real_task.task)
        p = purge.plan(self.c, bad)
        self.assertTrue(any('also protected' in r for r in p['refusals']))
        self.assertTrue(any('wrote kept document' in r for r in p['refusals']))
        with self.assertRaises(purge.Refused):
            purge.archive(p, self.root / 'archive2', bad)

    def test_a_test_task_that_wrote_a_kept_document_is_refused(self):
        with self.c.db.transaction() as db:                          # a second writer on the same document: shared history
            db.execute("INSERT INTO commits VALUES('commit-shared','event-real-one',1,2,'d','x','committed',?,'alice','now')", (self.test_task.task,))
        p = purge.plan(self.c, self.seed)
        self.assertTrue(any('shared' in r or 'wrote kept document' in r for r in p['refusals']))

    def test_kept_document_mentioning_a_removed_object_is_reported_and_refused_unless_allowed(self):
        self.write(self.real_task, 'event-real-two', 'See message-test-one for the source', 'commit-real-two')
        p = purge.plan(self.c, self.seed)
        self.assertEqual([r['document'] for r in p['references']], ['event-real-two'])
        self.assertTrue(any('mentions removed objects' in r for r in p['refusals']))
        allowed = dict(self.seed, allow_references_from=['event-real-two'])
        self.assertEqual(purge.plan(self.c, allowed)['refusals'], [])

    def test_an_unknown_delivery_row_owned_by_a_test_task_blocks(self):
        with self.c.db.transaction() as db:
            db.execute("UPDATE outbox SET state='unknown' WHERE id='out-test'")
        self.assertTrue(any('Unknown-delivery' in r for r in purge.plan(self.c, self.seed)['refusals']))

    def test_anything_changed_beyond_the_selection_rolls_the_whole_transaction_back(self):
        def also_delete_a_kept_row(db):
            db.execute("DELETE FROM outbox WHERE id='out-real'")
        with self.assertRaisesRegex(purge.Refused, 'beyond the selected rows'):
            self.archive_and_apply(extra_delete=also_delete_a_kept_row)
        self.assertEqual(len(self.rows('tasks')), 2)
        self.assertEqual({r['id'] for r in self.rows('outbox')}, {'out-test', 'out-real', 'out-unknown'})
        self.assertFalse([r for r in self.rows('audit') if r['action'] == 'test_data.purged'])

    def test_execution_paths_outside_the_allowed_roots_are_refused(self):
        outputs = self.root / 'outputs'
        (outputs / 'one').mkdir(parents=True)
        (outputs / 'one' / 'events.jsonl').write_text('{}', encoding='utf-8')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.test_task.task, str(outputs / 'job.json'), str(outputs / 'one'), 'finished', 'now'))
        self.assertTrue(any('not strictly inside an allowed output root' in r for r in purge.plan(self.c, self.seed)['refusals']))
        p = purge.plan(self.c, self.seed, [outputs])
        self.assertEqual(p['refusals'], [])
        purge.archive(p, self.root / 'archive3', self.seed)
        purge.apply(self.c, self.seed, self.root / 'archive3', [outputs], listening=self.never)
        self.assertFalse((outputs / 'one').exists())

    # ---- the four real failures found by the independent review (e146 0074工具审查) ----
    def doc_path(self, key):
        with self.c.db.transaction() as db:
            return self.c._path(db, db.execute('SELECT * FROM documents WHERE id=?', (key,)).fetchone())

    def test_a_neighbours_pending_file_in_the_same_directory_is_not_removed(self):
        neighbour = self.doc_path('message-test-one').parent / '.0123456789abcdef0123456789abcdef.pending'
        neighbour.write_text('belongs to a kept document commit', encoding='utf-8')
        self.assertNotIn(str(neighbour.resolve()), [f['path'] for f in purge.plan(self.c, self.seed)['files']])
        self.archive_and_apply()
        self.assertTrue(neighbour.exists())

    def test_a_file_added_after_archiving_blocks_apply_and_is_never_deleted(self):
        outputs = self.root / 'outputs'
        (outputs / 'one').mkdir(parents=True)
        (outputs / 'one' / 'events.jsonl').write_text('{}', encoding='utf-8')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.test_task.task, str(outputs / 'job.json'), str(outputs / 'one'), 'finished', 'now'))
        p = purge.plan(self.c, self.seed, [outputs])
        purge.archive(p, self.root / 'archive', self.seed)
        late = outputs / 'one' / 'added-after-archive.txt'
        late.write_text('new', encoding='utf-8')
        with self.assertRaisesRegex(purge.Refused, 'does not match'):
            purge.apply(self.c, self.seed, self.root / 'archive', [outputs], listening=self.never)
        self.assertTrue(late.exists())
        self.assertEqual(len(self.rows('tasks')), 2)

    def test_a_file_changed_after_the_final_check_is_left_in_place_and_reported(self):
        outputs = self.root / 'outputs'
        (outputs / 'one').mkdir(parents=True)
        victim = outputs / 'one' / 'events.jsonl'
        victim.write_text('{}', encoding='utf-8')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.test_task.task, str(outputs / 'job.json'), str(outputs / 'one'), 'finished', 'now'))
        p = purge.plan(self.c, self.seed, [outputs])
        purge.archive(p, self.root / 'archive', self.seed)
        result = purge.apply(self.c, self.seed, self.root / 'archive', [outputs], listening=self.never,
                             extra_delete=lambda db: victim.write_text('changed after verification', encoding='utf-8'))
        self.assertTrue(victim.exists())
        self.assertEqual(len(result['files_failed']), 1)

    def test_the_allowed_root_itself_or_a_kept_executions_directory_is_never_selected(self):
        outputs = self.root / 'outputs'
        outputs.mkdir()
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.test_task.task, str(outputs / 'job.json'), str(outputs), 'finished', 'now'))
        self.assertTrue(any('not strictly inside' in r for r in purge.plan(self.c, self.seed, [outputs])['refusals']))
        with self.c.db.transaction() as db:
            db.execute("DELETE FROM executions")
        (outputs / 'shared').mkdir()
        (outputs / 'shared' / 'a.txt').write_text('x', encoding='utf-8')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.test_task.task, str(outputs / 'j1'), str(outputs / 'shared'), 'finished', 'now'))
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.real_task.task, str(outputs / 'j2'), str(outputs / 'shared' / 'real'), 'finished', 'now'))
        self.assertTrue(any('overlaps something a kept row' in r for r in purge.plan(self.c, self.seed, [outputs])['refusals']))

    def test_a_linked_path_component_is_refused(self):
        outputs = self.root / 'outputs'
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'keep.txt').write_text('real data elsewhere', encoding='utf-8')
        outputs.mkdir()
        symlink_or_skip(self, elsewhere, outputs / 'one')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.test_task.task, str(outputs / 'j'), str(outputs / 'one'), 'finished', 'now'))
        p = purge.plan(self.c, self.seed, [outputs])
        self.assertTrue(p['refusals'])
        self.assertTrue((elsewhere / 'keep.txt').exists())

    def test_a_kept_tasks_read_of_a_removed_document_is_kept_and_never_selected(self):
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO task_reads VALUES(?,?)", (self.real_task.task, 'message-test-one'))
            db.execute("INSERT INTO citation_batches VALUES('cb-real',?,?,'{}','d')", (self.real_task.task, 'event-test-one'))
        p = purge.plan(self.c, self.seed)
        self.assertNotIn(self.real_task.task, [r['task'] for r in p['tables']['task_reads']])
        self.assertNotIn('cb-real', [r['id'] for r in p['tables']['citation_batches']])
        self.archive_and_apply()
        self.assertIn(('message-test-one',), [(r['document'],) for r in self.rows('task_reads') if r['task'] == self.real_task.task])
        self.assertEqual([r['id'] for r in self.rows('citation_batches')], ['cb-real'])

    def test_a_removed_message_handled_by_a_kept_task_or_a_kept_outbox_row_pointing_at_a_removed_document_refuses(self):
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO processing VALUES('mid-test-1','app','alice-private',?)", (self.real_task.task,))
        self.assertTrue(any('handled by kept task' in r for r in purge.plan(self.c, self.seed)['refusals']))
        with self.c.db.transaction() as db:
            db.execute("DELETE FROM processing")
            db.execute("UPDATE outbox SET payload_document='event-test-one' WHERE id='out-real'")
        self.assertTrue(any('Kept outbox row' in r for r in purge.plan(self.c, self.seed)['refusals']))

    # ---- junction/alias probes (review c8d29bd) and the partial-task split (0075) ----
    def test_an_alias_inside_the_root_pointing_at_a_real_directory_is_refused_by_name_not_resolved_away(self):
        outputs = self.root / 'outputs'
        (outputs / 'real').mkdir(parents=True)
        (outputs / 'real' / 'events.jsonl').write_text('{}', encoding='utf-8')
        symlink_or_skip(self, outputs / 'real', outputs / 'alias')               # a link inside the allowed root to a real directory inside it
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.test_task.task, str(outputs / 'j'), str(outputs / 'alias'), 'finished', 'now'))
        p = purge.plan(self.c, self.seed, [outputs])
        self.assertTrue(any('Linked path component' in r for r in p['refusals']))
        self.assertFalse([f for f in p['files'] if 'outputs' in f['path']])

    def test_a_link_inside_a_selected_directory_tree_is_refused_not_entered(self):
        outputs = self.root / 'outputs'
        (outputs / 'one').mkdir(parents=True)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'keep.txt').write_text('keep', encoding='utf-8')
        symlink_or_skip(self, elsewhere, outputs / 'one' / 'inner')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO executions VALUES(?,?,?,?,?,NULL,NULL)", (self.test_task.task, str(outputs / 'j'), str(outputs / 'one'), 'finished', 'now'))
        p = purge.plan(self.c, self.seed, [outputs])
        self.assertTrue(any('Linked entry refused' in r for r in p['refusals']))
        self.assertNotIn(str((elsewhere / 'keep.txt').resolve()), [f['path'] for f in p['files']])

    def partial_setup(self):
        """A test task that also wrote a kept (mixed) document and owns an unknown delivery: it cannot be removed whole."""
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO commits VALUES('commit-shared','event-real-one',1,2,'d','x','committed',?,'alice','now')", (self.test_task.task,))
            db.execute("UPDATE outbox SET task=?, payload_document='event-test-one' WHERE id='out-unknown'", (self.test_task.task,))
            db.execute("INSERT INTO skill_uses VALUES(?,?,?,?)", (self.test_task.task, 'loom-onboard', 'v2', 'loaded'))
        seed = json.loads(json.dumps(self.seed))
        seed['tasks'] = []
        seed['tasks_partial'] = [self.test_task.task]
        seed['protect']['documents'] = ['event-real-one', 'event-test-one']      # the unknown delivery's payload stays with its row
        return seed

    def test_partial_task_keeps_its_row_its_commits_on_kept_documents_and_the_unknown_delivery(self):
        seed = self.partial_setup()
        p = purge.plan(self.c, seed)
        self.assertEqual(p['refusals'], [])
        self.assertEqual([r['id'] for r in p['tables']['tasks']], [])
        self.archive_and_apply(seed)
        self.assertIn(self.test_task.task, [r['id'] for r in self.rows('tasks')])
        self.assertIn('commit-shared', [r['id'] for r in self.rows('commits')])
        self.assertIn('out-unknown', [r['id'] for r in self.rows('outbox')])
        self.assertIn('event-test-one', [r['id'] for r in self.rows('documents')])
        self.assertEqual([r for r in self.rows('skill_uses') if r['task'] == self.test_task.task], [])      # what it exclusively owned goes
        self.assertEqual([r for r in self.rows('task_reads') if r['task'] == self.test_task.task], [])

    def test_a_removed_task_that_wrote_a_kept_document_must_be_made_partial(self):
        seed = self.partial_setup()
        seed['tasks'], seed['tasks_partial'] = [self.test_task.task], []
        refusals = purge.plan(self.c, seed)['refusals']
        self.assertTrue(any('make it a partial task' in r for r in refusals))
        self.assertTrue(any('Unknown-delivery' in r for r in refusals))

    def test_a_kept_tasks_outbox_row_may_go_with_its_removed_payload_only_when_listed(self):
        with self.c.db.transaction() as db:
            db.execute("UPDATE outbox SET payload_document='event-test-one' WHERE id='out-real'")
        self.assertTrue(any('Kept outbox row' in r for r in purge.plan(self.c, self.seed)['refusals']))
        allowed = dict(self.seed, outbox_extra=['out-real'])
        p = purge.plan(self.c, allowed)
        self.assertEqual(p['refusals'], [])
        self.assertIn('out-real', [r['id'] for r in p['tables']['outbox']])

    # ---- production-path resolution when planning on a database copy (plan-copy-0076: 13 path refusals) ----
    def add_artifact_and_conflict(self):
        artifact = self.c.root / 'Artifacts' / 'report-x' / 'report.xlsx'
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b'xlsx bytes')
        intent = self.c.root / 'conflicts' / 'c1.md'
        intent.parent.mkdir(parents=True)
        intent.write_text('proposed text', encoding='utf-8')
        with self.c.db.transaction() as db:
            db.execute("INSERT INTO report_artifacts VALUES('message-test-one','src',1,'d','Artifacts/report-x/report.xlsx','d',10)")
            db.execute("INSERT INTO conflicts VALUES('c1','message-test-one',0,1,?,?,'alice','now')", (str(intent), self.test_task.task))
        return artifact, intent

    def test_relative_artifact_paths_and_conflict_intents_resolve_against_the_control_root(self):
        artifact, intent = self.add_artifact_and_conflict()
        p = purge.plan(self.c, self.seed)
        self.assertEqual(p['refusals'], [])
        listed = [f['path'] for f in p['files']]
        self.assertIn(str(artifact.resolve()), listed)
        self.assertIn(str(intent.resolve()), listed)

    def test_planning_on_a_database_copy_resolves_production_paths_without_widening_the_roots(self):
        import shutil
        artifact, intent = self.add_artifact_and_conflict()
        copy_root = self.root / 'copy-control'
        copy_root.mkdir()
        shutil.copy2(self.c.root / 'runtime.sqlite', copy_root / 'runtime.sqlite')
        copy = Control(copy_root)
        self.assertTrue(any('outside the allowed roots' in r for r in purge.plan(copy, self.seed)['refusals']))   # the old behaviour
        p = purge.plan(copy, self.seed, control_root=self.c.root)
        self.assertEqual(p['refusals'], [])
        self.assertIn(str(intent.resolve()), [f['path'] for f in p['files']])
        stranger = self.root / 'stranger.txt'                                   # a path outside every root is still refused
        stranger.write_text('x', encoding='utf-8')
        with copy.db.transaction() as db:
            db.execute("UPDATE report_artifacts SET path=?", (str(stranger),))
        self.assertTrue(any('outside the allowed roots' in r or 'resolves outside' in r for r in purge.plan(copy, self.seed, control_root=self.c.root)['refusals']))

    # ---- rehearsal isolation (rehearsal-safety-0084): Control(copy) isolates the database, not the files ----
    def make_copy(self, name='copy-control'):
        import shutil
        copy_root = self.root / name
        copy_root.mkdir()
        shutil.copy2(self.c.root / 'runtime.sqlite', copy_root / 'runtime.sqlite')
        return copy_root

    def source_roots(self):
        with self.c.db.transaction() as db:
            roots = sorted({Path(s['root']) for s in self.c.db.get_policy(db)['scopes'].values() if s.get('root')})
        return [r for r in roots if r.exists()]

    def test_a_plain_control_over_a_copy_writes_receipts_into_the_source_scope_roots(self):
        """The reviewer's finding, reproduced: this is why the rehearsal must not use a bare Control(copy)."""
        copy = Control(self.make_copy())
        before = purge.tree_fingerprint(self.source_roots())
        copy.task('alice', 'alice-private')                   # any audited action on the copy
        self.assertNotEqual(before, purge.tree_fingerprint(self.source_roots()))

    def test_rehearsal_control_keeps_the_complete_audit_in_the_rehearsal_dir_and_touches_no_source_file(self):
        before = purge.tree_fingerprint(self.source_roots())
        copy = purge.rehearsal_control(Control, self.make_copy(), self.c.root, self.root / 'rehearsal-writes')
        ctx = copy.task('alice', 'alice-private')
        copy.read(ctx, 'event-real-one')
        self.assertEqual(before, purge.tree_fingerprint(self.source_roots()))
        receipts = list((self.root / 'rehearsal-writes' / 'actions').rglob('*.json'))
        self.assertTrue(receipts)                             # nothing was switched off: every receipt exists, elsewhere

    def test_rehearsal_control_refuses_the_production_root_or_a_write_root_inside_it(self):
        with self.assertRaises(purge.Refused):
            purge.rehearsal_control(Control, self.c.root, self.c.root, self.root / 'w')
        with self.assertRaises(purge.Refused):
            purge.rehearsal_control(Control, self.make_copy(), self.c.root, self.c.root / 'inside')

    def test_rehearsal_apply_skips_the_port_guard_but_keeps_every_other_safeguard(self):
        copy = purge.rehearsal_control(Control, self.make_copy(), self.c.root, self.root / 'rehearsal-writes')
        p = purge.plan(copy, self.seed, control_root=self.c.root)
        self.assertEqual(p['refusals'], [])
        purge.archive(p, self.root / 'archive', self.seed)
        with self.assertRaisesRegex(purge.Refused, 'never removes files'):
            purge.apply(copy, self.seed, self.root / 'archive', listening=lambda: True, rehearsal=True, control_root=self.c.root)
        before = purge.tree_fingerprint(self.source_roots())
        result = purge.apply(copy, self.seed, self.root / 'archive', listening=lambda: True, remove_files=False, rehearsal=True, control_root=self.c.root)
        self.assertEqual(result['files_removed'], 0)
        self.assertEqual(before, purge.tree_fingerprint(self.source_roots()))             # not a single production file touched
        self.assertEqual(len(self.rows('tasks')), 2)                                      # the source database is unchanged
        with copy.db.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks WHERE id=?', (self.test_task.task,)).fetchone()[0], 0)

    def test_a_rehearsal_flag_without_rehearsal_control_or_on_the_production_root_is_refused(self):
        with self.assertRaisesRegex(purge.Refused, 'rehearsal_control'):
            purge.apply(self.c, self.seed, self.root / 'archive', listening=lambda: True, remove_files=False, rehearsal=True, control_root=self.c.root)
        copy = purge.rehearsal_control(Control, self.make_copy(), self.c.root, self.root / 'rehearsal-writes')
        with self.assertRaisesRegex(purge.Refused, 'must run on a copy'):
            purge.apply(copy, self.seed, self.root / 'archive', listening=lambda: True, remove_files=False, rehearsal=True, control_root=self.root / 'elsewhere')

    def test_a_production_apply_still_refuses_while_the_port_is_listening(self):
        p = purge.plan(self.c, self.seed)
        purge.archive(p, self.root / 'archive', self.seed)
        with self.assertRaisesRegex(purge.Refused, 'port is listening'):
            purge.apply(self.c, self.seed, self.root / 'archive', listening=lambda: True)

    # ---- link/junction/hard-link isolation, checked BEFORE any Control exists (review-rehearsal-0085) ----
    def spy(self):
        built = []

        def factory(root):
            built.append(root)
            return Control(root)
        return factory, built

    def source_state(self):
        return len(self.rows('tasks')), purge.tree_fingerprint(self.source_roots())

    def test_a_copy_root_that_is_a_link_to_the_production_root_is_refused_before_construction(self):
        before = self.source_state()
        alias = self.root / 'alias-of-production'
        symlink_or_skip(self, self.c.root, alias)
        factory, built = self.spy()
        with self.assertRaisesRegex(purge.Refused, 'link/junction'):
            purge.rehearsal_control(factory, alias, self.c.root, self.root / 'w')
        self.assertEqual(built, [])                                   # the database was never opened
        self.assertEqual(before, self.source_state())

    def test_a_link_anywhere_in_the_ancestor_chain_of_any_root_is_refused(self):
        real_parent = self.root / 'real-parent'
        (real_parent / 'copy').mkdir(parents=True)
        symlink_or_skip(self, real_parent, self.root / 'linked-parent')
        factory, built = self.spy()
        with self.assertRaisesRegex(purge.Refused, 'ancestors is a link'):
            purge.rehearsal_control(factory, self.root / 'linked-parent' / 'copy', self.c.root, self.root / 'w')
        with self.assertRaisesRegex(purge.Refused, 'link'):
            purge.rehearsal_control(factory, self.make_copy(), self.c.root, self.root / 'linked-parent' / 'w')
        self.assertEqual(built, [])

    def test_a_copy_database_hard_linked_to_the_production_database_is_refused_before_construction(self):
        copy_root = self.root / 'copy-control'
        copy_root.mkdir()
        os.link(self.c.root / 'runtime.sqlite', copy_root / 'runtime.sqlite')
        before = self.source_state()
        factory, built = self.spy()
        with self.assertRaisesRegex(purge.Refused, 'same file'):
            purge.rehearsal_control(factory, copy_root, self.c.root, self.root / 'w')
        self.assertEqual(built, [])
        self.assertEqual(before, self.source_state())

    def test_a_write_root_that_resolves_into_production_is_refused_and_a_late_link_cannot_redirect_receipts(self):
        factory, built = self.spy()
        symlink_or_skip(self, self.c.root, self.root / 'w-alias')
        with self.assertRaises(purge.Refused):
            purge.rehearsal_control(factory, self.make_copy('copy-a'), self.c.root, self.root / 'w-alias')
        self.assertEqual(built, [])
        copy = purge.rehearsal_control(Control, self.make_copy('copy-b'), self.c.root, self.root / 'rehearsal-writes')
        before = purge.tree_fingerprint(self.source_roots())
        (self.root / 'rehearsal-writes' / 'actions').mkdir(parents=True, exist_ok=True)
        task_dir = self.root / 'rehearsal-writes' / 'actions' / 'redirected'
        symlink_or_skip(self, self.source_roots()[0], task_dir)                    # a link planted after construction
        with self.assertRaises(purge.Refused):                           # the guard fires when the audited batch commits
            with copy.db.transaction() as db:
                db.execute("INSERT INTO tasks VALUES('redirected','alice-private','alice','x','done',NULL,NULL,1,NULL,0,'now')")
                copy.audit(db, 'tester', 'redirected', 'x', {})
        self.assertEqual(before, purge.tree_fingerprint(self.source_roots()))


if __name__ == '__main__':
    unittest.main()
