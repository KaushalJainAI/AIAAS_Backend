"""
Deleting and archiving an agent.

Two changes, one reason: getting an agent out of the way must not erase what
it did. `ExecutionLog.subagent` was CASCADE — nullable for exactly this case,
and still deleting every run and its recorded spend with the agent. And
`archived` could not be set, so delete was the only way out of the list.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APITestCase

from agents.models import SubAgent
from logs import revisions
from logs.models import ExecutionLog, SubAgentRevision


class DeleteKeepsHistoryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='keeper', password='pw')
        self.agent = SubAgent.objects.create(user=self.user, name='Reporter')
        revisions.record(self.agent, user=self.user, source='create')
        self.revision = SubAgentRevision.objects.get(subagent=self.agent)
        self.run = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, revision=self.revision,
            status='completed', tokens_used=1200,
        )

    def test_runs_survive_their_agent(self):
        self.agent.delete()
        self.run.refresh_from_db()
        self.assertIsNone(self.run.subagent_id)
        self.assertEqual(self.run.tokens_used, 1200)

    def test_the_configuration_a_run_used_survives_too(self):
        """Or the surviving run could no longer say why it behaved as it did."""
        self.agent.delete()
        self.run.refresh_from_db()
        self.assertEqual(self.run.revision_id, self.revision.id)
        self.assertEqual(self.run.revision.config.get('name'), 'Reporter')

    def test_a_deleted_agents_run_is_still_named(self):
        from logs.queries import _execution_row

        self.agent.delete()
        run = ExecutionLog.objects.select_related('subagent', 'revision').get(
            id=self.run.id)
        self.assertEqual(_execution_row(run)['workflow_name'], 'Reporter (deleted)')

    def test_deleting_two_agents_does_not_collide_on_revision_numbers(self):
        """Both orphans are `(NULL, 1)`; NULLs are distinct under the unique pair."""
        other = SubAgent.objects.create(user=self.user, name='Other')
        revisions.record(other, user=self.user, source='create')
        self.agent.delete()
        other.delete()
        self.assertEqual(
            SubAgentRevision.objects.filter(subagent__isnull=True).count(), 2)


class ArchiveTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='filer', password='pw')
        self.client.force_authenticate(self.user)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Old', allow_unattended=True)

    def _patch(self, **data):
        return self.client.patch(f'/api/orchestrator/agents/{self.agent.id}/',
                                 data, format='json')

    def test_archive_and_restore_round_trip(self):
        self.assertEqual(self._patch(status='archived').status_code, 200)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, 'archived')

        self.assertEqual(self._patch(status='active').status_code, 200)
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, 'active')

    def test_an_archived_agent_is_never_run_automatically(self):
        from agents.agent.runtime import AgentPaused, _check_status

        self.agent.status = 'archived'
        for caller in ('trigger', 'orchestrator'):
            with self.subTest(caller=caller), self.assertRaises(AgentPaused):
                _check_status(self.agent, caller)
        _check_status(self.agent, 'api')

    def test_the_builder_chat_cannot_archive(self):
        """Archiving is filing away; no description of a job implies it."""
        from agents.views.builder import KNOBS, Catalogue

        with self.assertRaises(Exception):
            KNOBS['status'].coerce('archived', Catalogue([], [], []))


class RestoreRevisionTests(APITestCase):
    """Rolling a configuration back is a save of the old snapshot, as a new revision."""

    def setUp(self):
        self.user = User.objects.create_user(username='undoer', password='pw')
        self.client.force_authenticate(self.user)
        created = self.client.post('/api/orchestrator/agents/', {
            'name': 'Digest', 'brief': 'Summarise the week.', 'temperature': 0.2,
        }, format='json')
        self.agent_id = created.data['id']
        self.client.patch(f'/api/orchestrator/agents/{self.agent_id}/', {
            'brief': 'Something else entirely.', 'temperature': 0.9,
        }, format='json')

    def _restore(self, number, agent_id=None):
        return self.client.post(
            f'/api/orchestrator/agents/{agent_id or self.agent_id}/revisions/{number}/restore/')

    def test_the_old_configuration_comes_back(self):
        response = self._restore(1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['brief'], 'Summarise the week.')
        self.assertEqual(response.data['temperature'], 0.2)

    def test_it_is_recorded_as_a_new_revision_not_a_rewrite(self):
        self._restore(1)
        revs = list(SubAgentRevision.objects.filter(subagent_id=self.agent_id)
                    .order_by('number').values_list('number', 'source'))
        self.assertEqual(revs, [(1, 'create'), (2, 'update'), (3, 'restore')])

    def test_status_is_not_rolled_back(self):
        """Undoing a config change is not a request to pause or unpause."""
        self.client.patch(f'/api/orchestrator/agents/{self.agent_id}/',
                          {'status': 'paused'}, format='json')
        self._restore(1)
        self.assertEqual(SubAgent.objects.get(id=self.agent_id).status, 'paused')

    def test_a_skill_since_deleted_refuses_rather_than_re_granting(self):
        from skills.models import Skill

        skill = Skill.objects.create(user=self.user, title='S', content='c')
        self.client.patch(f'/api/orchestrator/agents/{self.agent_id}/',
                          {'skills': [skill.id]}, format='json')
        number = SubAgentRevision.objects.filter(
            subagent_id=self.agent_id).order_by('-number').first().number
        self.client.patch(f'/api/orchestrator/agents/{self.agent_id}/',
                          {'skills': []}, format='json')
        skill.delete()

        response = self._restore(number)

        self.assertEqual(response.status_code, 400)
        self.assertIn('skills', response.data)

    def test_someone_elses_agent_is_not_found(self):
        other = User.objects.create_user(username='theirs', password='pw')
        theirs = SubAgent.objects.create(user=other, name='T')
        revisions.record(theirs, user=other, source='create')
        self.assertEqual(self._restore(1, agent_id=theirs.id).status_code, 404)


class ModelValidationTests(APITestCase):
    """A model id is checked at save, not discovered wrong at the first run."""

    def setUp(self):
        from llm.models import AIModel, AIProvider

        self.user = User.objects.create_user(username='modeller', password='pw')
        self.client.force_authenticate(self.user)
        openrouter, _ = AIProvider.objects.get_or_create(
            slug='openrouter', defaults={'name': 'OpenRouter'})
        nvidia, _ = AIProvider.objects.get_or_create(
            slug='nvidia', defaults={'name': 'NVIDIA'})
        AIModel.objects.update_or_create(
            value='vendor/good', defaults={'provider': openrouter, 'name': 'Good'})
        AIModel.objects.update_or_create(
            value='vendor/old', defaults={'provider': openrouter, 'name': 'Old',
                                          'is_active': False})
        AIModel.objects.update_or_create(
            value='nv/other', defaults={'provider': nvidia, 'name': 'Other'})

    def _create(self, **fields):
        return self.client.post('/api/orchestrator/agents/',
                                {'name': 'M', **fields}, format='json')

    def test_a_known_model_saves(self):
        response = self._create(provider='openrouter', model='vendor/good')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['model_status'], 'ok')

    def test_a_typo_is_refused_at_save(self):
        response = self._create(provider='openrouter', model='vendor/goood')
        self.assertEqual(response.status_code, 400)
        self.assertIn('model', response.data)

    def test_a_model_from_another_provider_says_which(self):
        response = self._create(provider='openrouter', model='nv/other')
        self.assertEqual(response.status_code, 400)
        self.assertIn('nvidia', str(response.data['model']))

    def test_a_retired_model_saves_but_is_flagged(self):
        """So an old agent stays editable while saying what needs changing."""
        response = self._create(provider='openrouter', model='vendor/old')
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data['model_status'], 'retired')

    def test_a_provider_with_no_catalogue_is_not_policed(self):
        """A fresh install has no rows; refusing everything there is worse."""
        response = self._create(provider='ollama', model='llama-anything')
        self.assertEqual(response.status_code, 201)

    def test_the_summary_model_is_checked_too(self):
        response = self._create(summaryProvider='openrouter', summaryModel='nope/nope')
        self.assertEqual(response.status_code, 400)
        self.assertIn('summaryModel', response.data)

    def test_a_retired_model_does_not_mint_a_revision(self):
        """`model_status` is observed, not configured — never in the snapshot."""
        from logs.revisions import snapshot

        created = self._create(provider='openrouter', model='vendor/old')
        agent = SubAgent.objects.get(id=created.data['id'])
        self.assertNotIn('model_status', snapshot(agent))


class RetiredFieldTests(APITestCase):
    """`reviewAgent` was stored, shown and read by nothing; it is gone."""

    def setUp(self):
        self.user = User.objects.create_user(username='tidy', password='pw')
        self.client.force_authenticate(self.user)

    def test_it_is_no_longer_on_the_wire(self):
        created = self.client.post('/api/orchestrator/agents/',
                                   {'name': 'A', 'reviewAgent': True}, format='json')
        self.assertEqual(created.status_code, 201)
        self.assertNotIn('reviewAgent', created.data)

    def test_an_old_snapshot_carrying_it_is_not_a_change(self):
        """Or every agent's next save would record "reviewAgent changed"."""
        from logs.revisions import diff

        old = {'name': 'A', 'reviewAgent': False}
        self.assertEqual(diff(old, {'name': 'A'}), {})


class ShareableKeysTests(APITestCase):
    def test_what_changes_how_it_runs_travels_and_retired_fields_do_not(self):
        from agents.publishing import SHAREABLE_KEYS

        for key in ('effort', 'outputContract', 'fanoutParallel',
                    'description', 'tags'):
            self.assertIn(key, SHAREABLE_KEYS)
        for key in ('reviewAgent', 'useOrgContext', 'workdir', 'venv',
                    'egress', 'trigger', 'delegatesTo'):
            self.assertNotIn(key, SHAREABLE_KEYS)
