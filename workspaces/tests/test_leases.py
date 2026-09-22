"""
C2 — file leases: writing takes a lock.

Only writes lock; readers are never blocked. Overlap is on glob prefixes, and
a conflicting claim is refused with the holder named — never dropped silently.
"""
from django.contrib.auth.models import User
from django.test import TestCase

from agents.models import SubAgent
from logs.models import ExecutionLog
from workspaces import leases
from workspaces.leases import LeaseConflict
from workspaces.models import CodeProject


class OverlapTests(TestCase):
    def test_exact_paths_overlap_only_when_equal(self):
        self.assertTrue(leases.overlaps('src/a.ts', 'src/a.ts'))
        self.assertFalse(leases.overlaps('src/a.ts', 'src/b.ts'))

    def test_subtree_claim_overlaps_a_file_inside_it(self):
        self.assertTrue(leases.overlaps('src/api/**', 'src/api/client.ts'))
        self.assertFalse(leases.overlaps('src/api/**', 'src/web/app.ts'))

    def test_two_subtrees_overlap_on_a_shared_prefix(self):
        self.assertTrue(leases.overlaps('src/**', 'src/api/**'))
        self.assertFalse(leases.overlaps('src/**', 'tests/**'))

    def test_bare_directory_claims_its_subtree(self):
        self.assertTrue(leases.covers('src/api', 'src/api/client.ts'))
        self.assertFalse(leases.covers('src/api', 'src/web/app.ts'))

    def test_glob_pattern_covers_matching_files(self):
        self.assertTrue(leases.covers('**/*.test.*', 'src/api/client.test.ts'))
        self.assertFalse(leases.covers('**/*.test.*', 'src/api/client.ts'))

    def test_dotdot_is_clamped_not_kept(self):
        self.assertEqual(leases.normalize_pattern('../src/a.ts'), 'src/a.ts')
        self.assertEqual(leases.normalize_pattern('/src//api/'), 'src/api')


class LeaseLifecycleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='lead', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Lead')
        self.project = CodeProject.objects.create(
            user=self.user, name='api', workspace_path='/home/user/projects/api')
        self.run_a = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='running')
        self.run_b = ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='running')

    def test_two_tasks_claiming_the_same_file_do_not_both_start(self):
        leases.acquire(self.project, self.run_a, ['src/api/client.ts'],
                       holder_label='Implementer #1', task_id='t3')
        with self.assertRaises(LeaseConflict) as raised:
            leases.acquire(self.project, self.run_b, ['src/api/client.ts'],
                           holder_label='Implementer #2', task_id='t4')
        # The refusal names the holder and the fix — a bare "denied" retries
        # until the iteration cap.
        self.assertIn('Implementer #1', str(raised.exception))
        self.assertIn('t3', str(raised.exception))

    def test_non_overlapping_claims_both_land(self):
        leases.acquire(self.project, self.run_a, ['src/api/**'],
                       holder_label='Implementer #1')
        leases.acquire(self.project, self.run_b, ['src/web/**'],
                       holder_label='Implementer #2')
        self.assertEqual(len(leases.live_leases(self.project.id)), 2)

    def test_a_holder_reacquiring_its_own_pattern_is_idempotent(self):
        leases.acquire(self.project, self.run_a, ['src/a.ts'], holder_label='A')
        leases.acquire(self.project, self.run_a, ['src/a.ts'], holder_label='A')
        self.assertEqual(len(leases.live_leases(self.project.id)), 1)

    def test_release_lets_the_other_task_start(self):
        leases.acquire(self.project, self.run_a, ['src/a.ts'], holder_label='A')
        leases.release_holder(self.run_a.id)
        leases.acquire(self.project, self.run_b, ['src/a.ts'], holder_label='B')
        self.assertEqual(leases.live_leases(self.project.id)[0].holder_id,
                         self.run_b.id)

    def test_expired_leases_do_not_block(self):
        from datetime import timedelta

        from django.utils import timezone

        leases.acquire(self.project, self.run_a, ['src/a.ts'], holder_label='A')
        from workspaces.models import CodeLease

        CodeLease.objects.filter(holder=self.run_a).update(
            expires_at=timezone.now() - timedelta(seconds=1))
        leases.acquire(self.project, self.run_b, ['src/a.ts'], holder_label='B')
        self.assertEqual(leases.live_leases(self.project.id)[0].holder_id,
                         self.run_b.id)

    def test_heartbeat_keeps_a_working_run_under_the_ttl(self):
        from workspaces.models import CodeLease

        leases.acquire(self.project, self.run_a, ['src/a.ts'], holder_label='A')
        leases.heartbeat(self.run_a.id)
        row = CodeLease.objects.get(holder=self.run_a)
        self.assertGreater(row.expires_at, row.acquired_at)
