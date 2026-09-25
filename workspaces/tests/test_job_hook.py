"""
S4/P2 (`docs/SECURITY_REVIEW_FIX_PLAN.md`): a workspace's job webhook wakes
only its owner's triggers, only for that job, and never blocks on the run.
"""
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from agents.models import SubAgent, Trigger
from workspaces.models import Workspace, WorkspaceJob


class JobHookTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', password='x')
        self.other = User.objects.create_user('other', password='x')
        self.ws = Workspace.objects.create(user=self.owner, secret='s' * 40)
        self.job = WorkspaceJob.objects.create(
            workspace=self.ws, user=self.owner, name='build', cmd='make')
        mine = SubAgent.objects.create(user=self.owner, name='Mine')
        theirs = SubAgent.objects.create(user=self.other, name='Theirs')
        self.waiting = Trigger.objects.create(
            subagent=mine, mode='event',
            config={'event': 'job.finished', 'job_id': self.job.id})
        self.unfiltered = Trigger.objects.create(
            subagent=mine, mode='event', config={'event': 'job.finished'})
        self.foreign = Trigger.objects.create(
            subagent=theirs, mode='event',
            config={'event': 'job.finished', 'job_id': self.job.id})

    def _post(self, secret, **body):
        return APIClient().post(f'/api/workspaces/hooks/{secret}/',
                                {'job_id': self.job.id, **body}, format='json')

    @patch('agents.scheduler.launch', new_callable=AsyncMock, return_value='fired')
    def test_only_the_owners_trigger_for_this_job_fires(self, launch):
        r = self._post(self.ws.secret, exit_code=0)
        self.assertEqual(r.status_code, 200)
        fired = [c.args[0].id for c in launch.await_args_list]
        self.assertEqual(fired, [self.waiting.id])
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'done')

    @patch('agents.scheduler.launch', new_callable=AsyncMock)
    def test_wrong_secret_is_a_404_and_touches_nothing(self, launch):
        self.assertEqual(self._post('nope' * 10).status_code, 404)
        launch.assert_not_awaited()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, 'running')

    @patch('agents.scheduler.launch', new_callable=AsyncMock)
    def test_an_unset_secret_never_matches(self, launch):
        Workspace.objects.filter(pk=self.ws.pk).update(secret='')
        from workspaces.views import _finish_job

        self.assertIsNone(_finish_job('', {'job_id': self.job.id}))
