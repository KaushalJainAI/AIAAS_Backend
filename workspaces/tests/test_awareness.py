"""
C3 — change awareness: who needs to know a file changed.

A writer's `_record_change` tells every *live* run that read the file or
claims it, through the steering mailbox as a notice (system context, never a
user turn), and tells the lead as one compact event. The stale-write guard —
not the notice — is what enforces, so a missed notice costs a round trip,
never correctness.
"""
from django.test import SimpleTestCase

from agents.agent import tasks as task_registry
from chat.turn import steering
from workspaces import awareness, reads


class AwarenessTests(SimpleTestCase):
    def setUp(self):
        steering.clear()
        reads.clear()
        task_registry.clear()
        self.addCleanup(steering.clear)
        self.addCleanup(reads.clear)
        self.addCleanup(task_registry.clear)

    def test_a_run_that_read_the_file_is_told(self):
        reads.record('worker-thread', 'src/a.ts', 'abc123')

        told = awareness.notify(
            7, 'src/a.ts', by_label='Implementer #1',
            writer_thread='other-thread', writer_execution_id='')

        self.assertEqual(told, 1)
        notice = steering.take_notices('worker-thread')
        self.assertIn('src/a.ts', notice)
        self.assertIn('Implementer #1', notice)

    def test_the_writer_is_never_told_about_its_own_change(self):
        reads.record('writer-thread', 'src/a.ts', 'abc123')

        told = awareness.notify(
            7, 'src/a.ts', by_label='Implementer #1',
            writer_thread='writer-thread', writer_execution_id='')

        self.assertEqual(told, 0)
        self.assertEqual(steering.take_notices('writer-thread'), '')

    def test_a_task_whose_claims_cover_the_file_is_told(self):
        bucket = task_registry._bucket('lead-thread')
        bucket['t2'] = task_registry.CodeTask(
            handle='t2', task_id='t2', title='Tests', agent_id=1,
            agent_name='Tester', label='Tester #1', project_id=7,
            project_name='api', claims=('src/**',),
            thread_id='tester-thread', execution_id='exec-t2')

        told = awareness.notify(
            7, 'src/a.ts', by_label='Implementer #1',
            writer_thread='impl-thread', writer_execution_id='exec-t1')

        self.assertEqual(told, 1)
        self.assertIn('src/a.ts', steering.take_notices('tester-thread'))

    def test_a_finished_task_is_not_told(self):
        bucket = task_registry._bucket('lead-thread')
        record = task_registry.CodeTask(
            handle='t2', task_id='t2', title='Tests', agent_id=1,
            agent_name='Tester', label='Tester #1', project_id=7,
            project_name='api', claims=('src/**',),
            thread_id='tester-thread', execution_id='exec-t2')
        bucket['t2'] = record
        task_registry.finish(record, status='done', answer='ok')

        told = awareness.notify(
            7, 'src/a.ts', by_label='Implementer #1',
            writer_thread='impl-thread', writer_execution_id='exec-t1')

        self.assertEqual(told, 0)

    def test_the_lead_gets_one_compact_event(self):
        bucket = task_registry._bucket('lead-thread')
        bucket['t1'] = task_registry.CodeTask(
            handle='t1', task_id='t1', title='Implement', agent_id=1,
            agent_name='Impl', label='Implementer #1', project_id=7,
            project_name='api', claims=('src/a.ts',),
            thread_id='impl-thread', execution_id='exec-t1')

        awareness.notify(
            7, 'src/a.ts', by_label='Implementer #1',
            writer_thread='impl-thread', writer_execution_id='exec-t1')

        events = task_registry.drain_events('lead-thread')
        self.assertEqual(len(events), 1)
        self.assertIn('src/a.ts', events[0])
        self.assertIn('Implementer #1', events[0])
        # Drained once: a lead that never waits must not accumulate them.
        self.assertEqual(task_registry.drain_events('lead-thread'), [])

    def test_a_user_edit_is_labelled_you(self):
        reads.record('worker-thread', 'src/a.ts', 'abc123')

        awareness.notify_user_edit(7, 'src/a.ts')

        self.assertIn('(by you)', steering.take_notices('worker-thread'))
