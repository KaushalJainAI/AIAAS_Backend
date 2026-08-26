"""
Configuration history: what the agent was when a run behaved that way.

`SubAgent` carried only `updated_at`, so there was no way to answer "it got
worse last Tuesday". These tests pin the two facts that make the answer
possible: a save that changes something writes a revision, and a run records
which revision it executed under.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from agents.models import SubAgent
from logs import queries, revisions
from logs.models import ExecutionLog, SubAgentRevision
from workflow_backend.thresholds import REVISION_VALUE_CHAR_LIMIT


def _config_payload(**overrides):
    """A complete AgentConfig body, as the builder POSTs it."""
    payload = {
        'name': 'Researcher',
        'brief': 'Find things on the web.',
        'provider': 'openrouter',
        'model': 'anthropic/claude-opus-5',
        'temperature': 0.2,
        'tools': {'webSearch': True},
        'autonomy': 'ask',
        'spendCapRupees': 500,
    }
    payload.update(overrides)
    return payload


class RevisionRecordingTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='author', password='pw')
        self.client.force_authenticate(user=self.user)

    def test_creating_an_agent_mints_revision_one(self):
        response = self.client.post(
            reverse('orchestrator:agent_list'), _config_payload(), format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        revision = SubAgentRevision.objects.get(subagent_id=response.data['id'])
        self.assertEqual(revision.number, 1)
        self.assertEqual(revision.source, 'create')
        self.assertEqual(revision.summary, 'Created')
        self.assertEqual(revision.config['model'], 'anthropic/claude-opus-5')
        self.assertEqual(revision.user, self.user)

    def test_a_change_mints_the_next_revision_with_a_diff(self):
        created = self.client.post(
            reverse('orchestrator:agent_list'), _config_payload(), format='json'
        )
        agent_id = created.data['id']

        response = self.client.patch(
            reverse('orchestrator:agent_detail', args=[agent_id]),
            {'autonomy': 'full'}, format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        latest = SubAgentRevision.objects.filter(subagent_id=agent_id).first()
        self.assertEqual(latest.number, 2)
        self.assertEqual(latest.source, 'update')
        self.assertEqual(latest.diff['autonomy'], {'from': 'ask', 'to': 'full'})
        self.assertIn('autonomy', latest.summary)

    def test_a_save_that_changes_nothing_mints_nothing(self):
        """The builder PATCHes constantly; forty identical entries is not a
        record of decisions."""
        created = self.client.post(
            reverse('orchestrator:agent_list'), _config_payload(), format='json'
        )
        agent_id = created.data['id']

        for _ in range(3):
            self.client.patch(
                reverse('orchestrator:agent_detail', args=[agent_id]),
                {'autonomy': 'ask'}, format='json',
            )

        self.assertEqual(
            SubAgentRevision.objects.filter(subagent_id=agent_id).count(), 1
        )

    def test_a_long_value_is_clipped_on_both_sides_of_the_diff(self):
        agent = SubAgent.objects.create(user=self.user, name='Wordy', prompt='short')
        revisions.record(agent, user=self.user, source='create')

        agent.prompt = 'z' * (REVISION_VALUE_CHAR_LIMIT + 2_000)
        agent.save()
        revision = revisions.record(agent, user=self.user)

        self.assertLess(len(revision.diff['brief']['to']),
                        REVISION_VALUE_CHAR_LIMIT + 60)


class RevisionDiffTests(APITestCase):
    """The diff helpers, tested directly — they decide what a timeline says."""

    def test_a_new_key_counts_as_a_change(self):
        changed = revisions.diff({'a': 1}, {'a': 1, 'b': 2})
        self.assertEqual(changed, {'b': {'from': None, 'to': 2}})

    def test_summary_names_what_moved_rather_than_counting_it(self):
        self.assertEqual(
            revisions.summarise({'model': {}, 'autonomy': {}}),
            'model, autonomy',
        )

    def test_a_long_summary_is_shortened_but_still_names_the_first_few(self):
        changed = {k: {} for k in
                   ('model', 'autonomy', 'tools', 'brief', 'egress', 'schedule')}
        summary = revisions.summarise(changed)
        self.assertTrue(summary.startswith('model, autonomy, tools'))
        self.assertIn('3 more', summary)

    def test_no_changes_reads_as_such(self):
        self.assertEqual(revisions.summarise({}), 'No changes')


class RunPinsItsRevisionTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='runner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Pinned', llm_model='m',
        )

    def test_a_run_records_the_configuration_it_executed_under(self):
        from asgiref.sync import async_to_sync

        from agents.agent.runtime import _open_log

        first = revisions.record(self.agent, user=self.user, source='create')
        log = async_to_sync(_open_log)(self.agent, self.user, 'go', 'manual', 't-1')
        self.assertEqual(log.revision_id, first.id)

        self.agent.guardrails = {'autonomy': 'full'}
        self.agent.save()
        second = revisions.record(self.agent, user=self.user)

        later = async_to_sync(_open_log)(self.agent, self.user, 'go', 'manual', 't-2')
        self.assertEqual(later.revision_id, second.id)
        # The earlier run still points at what it actually used. An agent
        # edited mid-flight must not retroactively rewrite its own history.
        log.refresh_from_db()
        self.assertEqual(log.revision_id, first.id)

    def test_an_agent_predating_revisions_gets_one_minted_on_first_run(self):
        from asgiref.sync import async_to_sync

        from agents.agent.runtime import _open_log

        self.assertFalse(SubAgentRevision.objects.filter(subagent=self.agent).exists())
        log = async_to_sync(_open_log)(self.agent, self.user, 'go', 'manual', 't-1')

        revision = SubAgentRevision.objects.get(subagent=self.agent)
        self.assertEqual(revision.number, 1)
        self.assertEqual(revision.source, 'backfill')
        self.assertIsNone(revision.user)
        self.assertEqual(log.revision_id, revision.id)


class RevisionEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reader', password='pw')
        self.other = User.objects.create_user(username='stranger', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(user=self.user, name='Timeline')

        self.first = revisions.record(self.agent, user=self.user, source='create')
        self.agent.llm_model = 'anthropic/claude-opus-5'
        self.agent.save()
        self.second = revisions.record(self.agent, user=self.user)

        ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, revision=self.second,
            status='completed', started_at=timezone.now(),
        )

    def test_timeline_is_newest_first_with_diffs_and_run_counts(self):
        url = reverse('logs:revision_list', args=[self.agent.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        rows = response.data['results']
        self.assertEqual([r['number'] for r in rows], [2, 1])
        self.assertIn('model', rows[0]['diff'])
        self.assertEqual(rows[0]['changed_by'], 'reader')
        # How many runs exercised this configuration — the number that says
        # whether a change has been tried enough to judge.
        self.assertEqual(rows[0]['run_count'], 1)
        self.assertEqual(rows[1]['run_count'], 0)
        self.assertEqual(response.data['count'], 2)
        self.assertFalse(response.data['truncated'])

    def test_a_long_timeline_is_capped_and_says_so(self):
        """A tuned agent accumulates revisions without limit, and a cut
        timeline and a short one must not look alike."""
        url = reverse('logs:revision_list', args=[self.agent.id])
        with mock.patch.object(queries, 'REVISION_TIMELINE_LIMIT', 1):
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)
        self.assertEqual([r['number'] for r in response.data['results']], [2])
        self.assertTrue(response.data['truncated'])

    def test_one_revision_carries_its_full_config(self):
        url = reverse('logs:revision_detail', args=[self.agent.id, 2])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['config']['model'],
                         'anthropic/claude-opus-5')

    def test_a_missing_revision_is_404(self):
        url = reverse('logs:revision_detail', args=[self.agent.id, 99])
        self.assertEqual(self.client.get(url).status_code,
                         status.HTTP_404_NOT_FOUND)

    def test_another_users_agent_has_no_readable_history(self):
        theirs = SubAgent.objects.create(user=self.other, name='Theirs')
        revisions.record(theirs, user=self.other, source='create')

        url = reverse('logs:revision_list', args=[theirs.id])
        self.assertEqual(self.client.get(url).status_code,
                         status.HTTP_404_NOT_FOUND)

    def test_requires_authentication(self):
        self.client.force_authenticate(user=None)
        url = reverse('logs:revision_list', args=[self.agent.id])
        self.assertIn(
            self.client.get(url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
