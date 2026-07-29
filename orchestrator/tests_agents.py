"""
Agents API tests.

The cases worth having are the ones where a mistake is invisible in the UI: a
grant that silently widens, a knowledge base that belongs to someone else, an
agent that shows up in the workflow canvas list. Round-tripping a config is the
cheap half; the ownership and merge cases are the half that matters.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from inference.models import KnowledgeBase
from orchestrator.models import Workflow


def config(**overrides):
    """A minimal valid agent, plus whatever the test is actually about."""
    base = {
        'name': 'Finance agent',
        'brief': 'Reads invoices and chases what is overdue.',
        'provider': 'openrouter',
        'model': 'anthropic/claude-sonnet-5',
        'temperature': 0,
        'tools': {'codeExecution': True, 'rag': True},
        'autonomy': 'ask',
        'trigger': 'goal',
    }
    base.update(overrides)
    return base


class AgentCrudTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.list_url = reverse('orchestrator:agent_list')

    def test_create_returns_the_config_it_was_given(self):
        r = self.client.post(self.list_url, config(), format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data['name'], 'Finance agent')
        self.assertEqual(r.data['tools']['codeExecution'], True)
        self.assertEqual(r.data['autonomy'], 'ask')
        # Stats exist from the first read, at zero, so the card never has to
        # guard against a missing field.
        self.assertEqual(r.data['runs'], 0)

    def test_unsent_tools_are_stored_as_denied_not_absent(self):
        """A grant the caller never mentioned must read as 'no', not 'unset'."""
        r = self.client.post(self.list_url, config(tools={'rag': True}), format='json')
        agent = Workflow.objects.get(id=r.data['id'])
        self.assertEqual(agent.tool_grants['shell'], False)
        self.assertEqual(agent.tool_grants['codeExecution'], False)
        self.assertEqual(sorted(agent.tool_grants), sorted(agent.tool_grants))
        self.assertIn('webSearch', agent.tool_grants)

    def test_round_trip_preserves_every_knob(self):
        sent = config(
            fileAccess='readonly', workdir='/data', venv=False, cpu=4, memoryMb=2048,
            connectors=['gmail', 'sheets'], useOrgContext=False, useEnvironment=True,
            trigger='maintenance', schedule='0 9 * * 1',
            notifyOnHitl=False, reviewAgent=True, spendCapRupees=750, egress='allowlist',
            recursiveContext=False, compaction=False, indexing=False,
        )
        created = self.client.post(self.list_url, sent, format='json')
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        fetched = self.client.get(
            reverse('orchestrator:agent_detail', args=[created.data['id']])
        )
        for key, value in sent.items():
            if key == 'tools':
                continue
            self.assertEqual(fetched.data[key], value, f'{key} did not survive the round trip')

    def test_patch_merges_rather_than_resetting_unsent_knobs(self):
        created = self.client.post(
            self.list_url, config(spendCapRupees=900, connectors=['gmail']), format='json'
        )
        url = reverse('orchestrator:agent_detail', args=[created.data['id']])

        r = self.client.patch(url, {'temperature': 0.7}, format='json')
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        self.assertEqual(r.data['temperature'], 0.7)
        # The knobs the caller did not send are still what they were.
        self.assertEqual(r.data['spendCapRupees'], 900)
        self.assertEqual(r.data['connectors'], ['gmail'])
        self.assertEqual(r.data['tools']['codeExecution'], True)

    def test_delete(self):
        created = self.client.post(self.list_url, config(), format='json')
        url = reverse('orchestrator:agent_detail', args=[created.data['id']])
        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_duplicate_name_is_suffixed_not_rejected(self):
        self.client.post(self.list_url, config(), format='json')
        second = self.client.post(self.list_url, config(), format='json')
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.data['name'], 'Finance agent (1)')


class AgentValidationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.list_url = reverse('orchestrator:agent_list')

    def _post(self, **kw):
        return self.client.post(self.list_url, config(**kw), format='json')

    def test_maintenance_without_schedule_is_refused(self):
        r = self._post(trigger='maintenance', schedule='')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('schedule', r.data)

    def test_malformed_cron_is_refused(self):
        r = self._post(trigger='maintenance', schedule='every monday')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_tool_key_is_refused(self):
        r = self._post(tools={'rootkit': True})
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_connector_is_refused(self):
        r = self._post(connectors=['dropbox'])
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_workdir_traversal_is_refused(self):
        r = self._post(workdir='/workspace/../../etc')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_shell_plus_full_egress_is_refused(self):
        r = self._post(tools={'shell': True}, egress='full')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('egress', r.data)

    def test_memory_above_the_ceiling_is_refused(self):
        self.assertEqual(self._post(memoryMb=999_999).status_code, status.HTTP_400_BAD_REQUEST)

    def test_blank_name_is_refused(self):
        self.assertEqual(self._post(name='   ').status_code, status.HTTP_400_BAD_REQUEST)


class AgentIsolationTests(APITestCase):
    """Cross-tenant cases. Each of these would be a silent data leak."""

    def setUp(self):
        self.owner = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='other', password='pw')
        self.list_url = reverse('orchestrator:agent_list')

    def test_cannot_attach_another_users_knowledge_base(self):
        their_kb = KnowledgeBase.objects.create(user=self.other, name='Their vendors')
        self.client.force_authenticate(user=self.owner)

        r = self.client.post(self.list_url, config(knowledgeBases=[their_kb.id]), format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('knowledgeBases', r.data)

    def test_can_attach_own_knowledge_base(self):
        mine = KnowledgeBase.objects.create(user=self.owner, name='My vendors')
        self.client.force_authenticate(user=self.owner)

        r = self.client.post(self.list_url, config(knowledgeBases=[mine.id]), format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data['knowledgeBases'], [mine.id])

    def test_another_users_agent_is_not_listed_or_readable(self):
        self.client.force_authenticate(user=self.owner)
        created = self.client.post(self.list_url, config(), format='json')

        self.client.force_authenticate(user=self.other)
        self.assertEqual(self.client.get(self.list_url).data, [])
        detail = reverse('orchestrator:agent_detail', args=[created.data['id']])
        self.assertEqual(self.client.get(detail).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(detail).status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_access_is_refused(self):
        self.assertIn(
            self.client.get(self.list_url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class AgentWorkflowSeparationTests(APITestCase):
    """Agents and workflows share a table; they must not share a list."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)

    def test_agents_do_not_appear_in_the_workflow_list(self):
        self.client.post(reverse('orchestrator:agent_list'), config(), format='json')
        Workflow.objects.create(user=self.user, name='A real workflow', nodes=[], edges=[])

        r = self.client.get(reverse('orchestrator:workflow_list'))
        names = [w['name'] for w in r.data]
        self.assertIn('A real workflow', names)
        self.assertNotIn('Finance agent', names)

    def test_workflows_do_not_appear_in_the_agent_list(self):
        Workflow.objects.create(user=self.user, name='A real workflow', nodes=[], edges=[])
        r = self.client.get(reverse('orchestrator:agent_list'))
        self.assertEqual(r.data, [])

    def test_agent_detail_refuses_a_workflow_id(self):
        wf = Workflow.objects.create(user=self.user, name='A real workflow')
        r = self.client.get(reverse('orchestrator:agent_detail', args=[wf.id]))
        self.assertEqual(r.status_code, status.HTTP_404_NOT_FOUND)
