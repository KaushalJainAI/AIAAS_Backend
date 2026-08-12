"""
Agent runtime tests.

The cases here are the ones docs/AGENT_TEMPLATES.md §10 names as the way this
design fails: the permissions screen drifting from what the runtime enforces.
So the bulk of them are about a denied grant actually failing — both at
advertising time and, more importantly, at call time, because a model can name
a tool it was never offered.
"""
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from orchestrator.agent_runtime import (
    GRANT_TOOLS,
    UNSERVED_GRANTS,
    AgentRunRefused,
    AgentToolbox,
    build_system_prompt,
    check_guardrails,
    sensitive_tools_for,
)
from orchestrator.models import Workflow


def toolbox(**grants) -> AgentToolbox:
    from orchestrator.agents import TOOL_KEYS

    full = {k: bool(grants.get(k, False)) for k in TOOL_KEYS}
    return AgentToolbox(grants=full, user_id=1)


class GrantMappingTests(SimpleTestCase):
    def test_every_grant_key_is_known_to_the_api(self):
        # A grant the runtime maps but the API rejects would be dead code; a
        # grant the API accepts but the runtime ignores is worse — the
        # permissions screen would show a capability nothing implements.
        from orchestrator.agents import TOOL_KEYS

        self.assertEqual(set(GRANT_TOOLS) | UNSERVED_GRANTS, TOOL_KEYS)

    def test_no_grants_means_no_tools_beyond_the_harmless_ones(self):
        names = toolbox().allowed_names
        self.assertEqual(names, frozenset({'get_current_time'}))

    def test_a_grant_unlocks_exactly_its_own_tools(self):
        names = toolbox(webSearch=True).allowed_names
        self.assertIn('web_search', names)
        self.assertNotIn('scrape_webpage', names)
        self.assertNotIn('knowledge_base_search', names)

    def test_unserved_grants_are_reported_not_silently_dropped(self):
        agent = Workflow(tool_grants={'shell': True, 'fileOps': True, 'rag': True})
        box = AgentToolbox.for_agent(agent, user_id=1)
        self.assertEqual(box.unserved, ('fileOps', 'shell'))
        self.assertNotIn('shell', box.allowed_names)


class DispatchEnforcementTests(SimpleTestCase):
    """Advertising is one thing; a live dispatch is the one that matters."""

    def test_ungranted_tool_is_refused_at_call_time(self):
        result = async_to_sync(toolbox().dispatch)('web_search', {'query': 'x'}, {})
        self.assertIn('not available to this agent', result)

    def test_ungranted_code_execution_is_refused(self):
        result = async_to_sync(toolbox().dispatch)('execute_python', {'code': '1'}, {})
        self.assertIn('not available to this agent', result)

    def test_ungranted_mcp_tool_is_refused(self):
        with patch('mcp_integration.tool_provider.is_mcp_tool', return_value=True):
            result = async_to_sync(toolbox().dispatch)('mcp_7_send', {}, {})
        self.assertIn('not available to this agent', result)

    def test_granted_tool_reaches_the_registry(self):
        with patch('chat.tools.ToolExecutor.execute') as execute:
            async def ok(*a, **k):
                return 'results'
            execute.side_effect = ok
            result = async_to_sync(toolbox(webSearch=True).dispatch)(
                'web_search', {'query': 'x'}, {'user_id': 1}
            )
        self.assertEqual(result, 'results')

    def test_granted_code_runs_in_the_sandbox(self):
        result = async_to_sync(toolbox(codeExecution=True).dispatch)(
            'execute_python', {'code': 'result = 6 * 7'}, {}
        )
        self.assertIn('42', result)

    def test_sandbox_refuses_the_escapes_it_is_meant_to(self):
        result = async_to_sync(toolbox(codeExecution=True).dispatch)(
            'execute_python', {'code': 'import os\nresult = os.listdir("/")'}, {}
        )
        self.assertTrue(result.startswith('Error'), result)

    def test_empty_code_is_rejected_rather_than_run(self):
        result = async_to_sync(toolbox(codeExecution=True).dispatch)(
            'execute_python', {'code': '   '}, {}
        )
        self.assertIn("'code' is required", result)


class DescriptorTests(SimpleTestCase):
    def test_descriptors_only_contain_granted_tools(self):
        names = {
            d['function']['name']
            for d in async_to_sync(toolbox(rag=True).descriptors)()
        }
        self.assertIn('knowledge_base_search', names)
        self.assertNotIn('web_search', names)
        self.assertNotIn('execute_python', names)

    def test_code_execution_adds_the_python_tool(self):
        names = {
            d['function']['name']
            for d in async_to_sync(toolbox(codeExecution=True).descriptors)()
        }
        self.assertIn('execute_python', names)

    def test_mcp_tools_appear_only_when_granted(self):
        async def descriptors(_user):
            return [{'type': 'function', 'function': {'name': 'mcp_1_send'}}]

        with patch('mcp_integration.tool_provider.MCPToolProvider.'
                   'get_openai_tool_descriptors', side_effect=descriptors):
            without = {d['function']['name']
                       for d in async_to_sync(toolbox().descriptors)()}
            with_grant = {d['function']['name']
                          for d in async_to_sync(toolbox(mcp=True).descriptors)()}

        self.assertNotIn('mcp_1_send', without)
        self.assertIn('mcp_1_send', with_grant)

    def test_a_dead_mcp_server_degrades_rather_than_fails_the_run(self):
        async def boom(_user):
            raise ConnectionError('server down')

        with patch('mcp_integration.tool_provider.MCPToolProvider.'
                   'get_openai_tool_descriptors', side_effect=boom):
            descriptors = async_to_sync(toolbox(mcp=True, rag=True).descriptors)()

        self.assertIn('knowledge_base_search',
                      {d['function']['name'] for d in descriptors})


class AutonomyTests(SimpleTestCase):
    def test_full_autonomy_pauses_on_nothing(self):
        self.assertEqual(sensitive_tools_for('full', toolbox(webSearch=True)),
                         frozenset())

    def test_review_pauses_on_every_granted_tool(self):
        box = toolbox(webSearch=True, rag=True)
        self.assertTrue(box.allowed_names <= sensitive_tools_for('review', box))

    def test_ask_pauses_on_code_execution(self):
        self.assertIn('execute_python', sensitive_tools_for('ask', toolbox()))


class SystemPromptTests(SimpleTestCase):
    def _prompt(self, agent, **gathered):
        base = {'skills': [], 'knowledge_bases': [], 'ctx': {}}
        base.update(gathered)
        return build_system_prompt(agent, base)

    def test_prompt_states_the_granted_capabilities(self):
        agent = Workflow(name='Finance', context='Chase invoices.',
                         tool_grants={'rag': True, 'webSearch': False},
                         guardrails={'autonomy': 'ask', 'egress': 'none'})
        prompt = self._prompt(agent)
        self.assertIn('Chase invoices.', prompt)
        self.assertIn('rag', prompt)
        self.assertIn('no network access', prompt)

    def test_prompt_names_a_grant_the_runtime_will_not_serve(self):
        # Otherwise the agent plans around a shell it will never be handed and
        # the user reads the failure as the model being stupid.
        agent = Workflow(name='A', context='b', tool_grants={'shell': True},
                         guardrails={})
        self.assertIn('shell', self._prompt(agent))

    def test_skills_are_inlined_into_the_brief(self):
        agent = Workflow(name='A', context='b', tool_grants={}, guardrails={})
        prompt = self._prompt(agent, skills=[('GSTIN', 'Check the checksum.')])
        self.assertIn('Check the checksum.', prompt)


class SpendCapTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.agent = Workflow.objects.create(
            user=self.user, name='A', kind='agent', nodes=[], edges=[],
            guardrails={'spendCapRupees': 100},
        )

    def _log(self, credits):
        from logs.models import ExecutionLog
        ExecutionLog.objects.create(workflow=self.agent, user=self.user,
                                    status='completed', credits_used=credits)

    def test_under_the_cap_runs(self):
        self._log(30)
        async_to_sync(check_guardrails)(self.agent, self.user)  # does not raise

    def test_at_the_cap_is_refused_before_the_model_is_called(self):
        self._log(100)
        with self.assertRaises(AgentRunRefused):
            async_to_sync(check_guardrails)(self.agent, self.user)

    def test_no_cap_means_no_check(self):
        self.agent.guardrails = {'spendCapRupees': 0}
        self._log(9999)
        async_to_sync(check_guardrails)(self.agent, self.user)


class ExecuteEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='other', password='pw')
        self.client.force_authenticate(user=self.user)
        self.agent = Workflow.objects.create(
            user=self.user, name='A', kind='agent', nodes=[], edges=[],
            context='Do the thing.', tool_grants={'rag': True}, guardrails={},
        )
        self.url = reverse('orchestrator:agent_execute', args=[self.agent.id])

    def test_a_run_returns_the_answer(self):
        from orchestrator.agent_runtime import AgentRun

        async def fake_run(agent, goal, **kwargs):
            return AgentRun(execution_id='e1', answer='Done.', thinking='',
                            tool_trace=[], tokens=12, awaiting_approval=False,
                            unserved_grants=('shell',), duration_ms=5)

        with patch('orchestrator.agent_runtime.run_agent', side_effect=fake_run):
            response = self.client.post(self.url, {'goal': 'go'}, format='json')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['answer'], 'Done.')
        # Named so the caller can tell the user a configured capability was not
        # honoured, rather than leaving them to infer it.
        self.assertEqual(response.data['unserved_grants'], ['shell'])

    def test_a_run_opens_and_closes_an_execution_log(self):
        """The ledger the `runs` / `unattended` / `spend` stats are counted from.

        Exercised against the real helpers rather than through the view: the
        point is that the run is recorded, and a fake that logged for us would
        be testing the fake.
        """
        from logs.models import ExecutionLog
        from orchestrator.agent_runtime import _close_log, _open_log

        log = async_to_sync(_open_log)(self.agent, self.user, 'go', 'manual')
        self.assertEqual(log.status, 'running')
        self.assertEqual(log.input_data, {'goal': 'go'})

        async_to_sync(_close_log)(log, status='completed',
                                  result={'answer': 'Done.'}, tokens=12)

        stored = ExecutionLog.objects.get(id=log.id)
        self.assertEqual(stored.status, 'completed')
        self.assertEqual(stored.tokens_used, 12)
        self.assertIsNotNone(stored.completed_at)
        self.assertGreaterEqual(stored.duration_ms, 0)

    def test_a_blank_goal_is_rejected(self):
        response = self.client.post(self.url, {'goal': '   '}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_someone_elses_agent_is_not_runnable(self):
        self.client.force_authenticate(user=self.other)
        response = self.client.post(self.url, {'goal': 'go'}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_a_workflow_is_not_runnable_through_the_agent_route(self):
        wf = Workflow.objects.create(user=self.user, name='W', kind='workflow',
                                     nodes=[], edges=[])
        url = reverse('orchestrator:agent_execute', args=[wf.id])
        response = self.client.post(url, {'goal': 'go'}, format='json')
        self.assertEqual(response.status_code, 404)

    def test_spend_cap_refusal_is_reported_as_payment_required(self):
        async def refuse(*a, **k):
            raise AgentRunRefused('cap reached')

        with patch('orchestrator.agent_runtime.run_agent', side_effect=refuse):
            response = self.client.post(self.url, {'goal': 'go'}, format='json')

        self.assertEqual(response.status_code, 402)
        self.assertIn('cap reached', response.data['error'])
