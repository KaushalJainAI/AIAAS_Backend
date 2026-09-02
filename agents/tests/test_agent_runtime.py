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

from agents.agent.runtime import (
    GRANT_TOOLS,
    UNSERVED_GRANTS,
    AgentRunRefused,
    AgentToolbox,
    build_system_prompt,
    check_guardrails,
    sensitive_tools_for,
)
from agents.models import SubAgent


def toolbox(_file_scope=None, **grants) -> AgentToolbox:
    from agents.views.agents import TOOL_KEYS

    full = {k: bool(grants.get(k, False)) for k in TOOL_KEYS}
    return AgentToolbox(grants=full, user_id=1, file_scope=_file_scope)


class GrantMappingTests(SimpleTestCase):
    def test_every_grant_key_is_known_to_the_api(self):
        # A grant the runtime maps but the API rejects would be dead code; a
        # grant the API accepts but the runtime ignores is worse — the
        # permissions screen would show a capability nothing implements.
        from agents.views.agents import TOOL_KEYS

        self.assertEqual(set(GRANT_TOOLS) | UNSERVED_GRANTS, TOOL_KEYS)

    def test_no_grants_means_no_tools_beyond_the_harmless_ones(self):
        # The retrieval pair is here for the same reason `get_current_time` is:
        # neither reaches anything the user owns. They read back *this run's own
        # transcript* — text the agent was already shown and has since had
        # curated away — so gating them behind a grant would mean an agent could
        # be told what it is missing and given no way to fetch it, which is what
        # actually shipped before curation existed. They are still only
        # *offered* once the run has stored something; see
        # `RetrievalToolAdvertisementTests`.
        names = toolbox().allowed_names
        self.assertEqual(
            names,
            frozenset({'get_current_time', 'read_tool_output', 'recall_context'}),
        )

    def test_a_grant_unlocks_exactly_its_own_tools(self):
        names = toolbox(webSearch=True).allowed_names
        self.assertIn('web_search', names)
        self.assertNotIn('scrape_webpage', names)
        self.assertNotIn('knowledge_base_search', names)

    def test_unserved_grants_are_reported_not_silently_dropped(self):
        agent = SubAgent(tool_grants={'shell': True, 'fileOps': True, 'rag': True})
        box = AgentToolbox.for_agent(agent, user_id=1)
        # `fileOps` used to be here beside `shell`. It is served now — see
        # `AgentFileAccessTests` — so `shell` is the only grant the runtime
        # still declines to honour.
        self.assertEqual(box.unserved, ('shell',))
        self.assertNotIn('shell', box.allowed_names)


class AgentFileAccessTests(SimpleTestCase):
    """The `fileOps` grant and the `fileAccess` setting are two switches, and
    the tools appear only when both are on.

    Splitting them is what lets one agent read the user's whole tree while
    another only ever sees its own folder, without a second grant key for every
    combination. The failure this pins is the quiet one: a grant left on while
    access is 'none', which would otherwise advertise five tools that refuse
    every call.
    """

    FILE_TOOLS = frozenset(GRANT_TOOLS['fileOps'])

    def _scope(self):
        # A scope object is all `allowed_names` inspects — it never dereferences
        # it — so this stays a SimpleTestCase with no database.
        return object()

    def test_grant_without_a_scope_offers_nothing(self):
        box = toolbox(fileOps=True)
        self.assertFalse(self.FILE_TOOLS & box.allowed_names)

    def test_grant_with_a_scope_offers_all_five(self):
        box = toolbox(self._scope(), fileOps=True)
        self.assertTrue(self.FILE_TOOLS <= box.allowed_names)

    def test_a_scope_without_the_grant_offers_nothing(self):
        box = toolbox(self._scope())
        self.assertFalse(self.FILE_TOOLS & box.allowed_names)

    def test_file_tools_are_refused_at_dispatch_without_the_grant(self):
        result = async_to_sync(toolbox(self._scope()).dispatch)(
            'write_file', {'path': 'x', 'content': 'y'}, {})
        self.assertIn('not available to this agent', result)

    def test_writes_and_deletes_are_sensitive_reads_are_not(self):
        # An agent on 'ask' should pause before changing the user's files and
        # not before looking at them, or the prompts become noise people click
        # through — which is what makes the remaining ones worthless.
        from chat.tools import SENSITIVE_TOOLS

        for name in ('write_file', 'delete_file', 'make_directory'):
            self.assertIn(name, SENSITIVE_TOOLS)
        for name in ('read_file', 'list_files'):
            self.assertNotIn(name, SENSITIVE_TOOLS)


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
        with patch('chat.tools.execute_tool') as execute:
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


class RetrievalToolAdvertisementTests(TestCase):
    """`read_tool_output` and `recall_context` are dispatchable always and
    offered only once the run has actually stored something.

    The split is deliberate. Advertising them on an empty run would put two
    tools in front of the model that can only answer "there is nothing here",
    and an advertised tool that cannot do anything is one the model plans
    around. Refusing them at dispatch would be worse in the other direction: the
    condition is a database read, and a model that names a tool it was offered a
    moment ago must not be turned away because a row expired in between.
    """

    RETRIEVAL = {'read_tool_output', 'recall_context'}

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('retriever', 'r@example.com', 'x')

    def _names(self, session_key):
        box = AgentToolbox(grants={}, user_id=self.user.id, session_key=session_key)
        return {d['function']['name']
                for d in async_to_sync(box.descriptors)()}

    def test_withheld_until_something_is_stored(self):
        self.assertFalse(self.RETRIEVAL & self._names('run-empty'))

    def test_offered_once_the_run_has_archived(self):
        from django.utils import timezone
        from chat.models import ToolOutput

        ToolOutput.objects.create(
            user=self.user, session_key='run-full',
            tool_name='context:archive:web_search', content='x' * 100,
            total_chars=100,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )
        self.assertTrue(self.RETRIEVAL <= self._names('run-full'))

    def test_one_runs_archive_is_not_offered_to_another(self):
        from django.utils import timezone
        from chat.models import ToolOutput

        ToolOutput.objects.create(
            user=self.user, session_key='run-a', tool_name='context:archive:t',
            content='x' * 100, total_chars=100,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )
        self.assertFalse(self.RETRIEVAL & self._names('run-b'))


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
        async def descriptors(_user, _server_ids=None):
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
        async def boom(_user, _server_ids=None):
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
        agent = SubAgent(name='Finance', prompt='Chase invoices.',
                         tool_grants={'rag': True, 'webSearch': False},
                         guardrails={'autonomy': 'ask', 'egress': 'none'})
        prompt = self._prompt(agent)
        self.assertIn('Chase invoices.', prompt)
        self.assertIn('rag', prompt)
        self.assertIn('no network access', prompt)

    def test_prompt_names_a_grant_the_runtime_will_not_serve(self):
        # Otherwise the agent plans around a shell it will never be handed and
        # the user reads the failure as the model being stupid.
        agent = SubAgent(name='A', prompt='b', tool_grants={'shell': True},
                         guardrails={})
        self.assertIn('shell', self._prompt(agent))

    def test_skills_are_inlined_into_the_brief(self):
        agent = SubAgent(name='A', prompt='b', tool_grants={}, guardrails={})
        prompt = self._prompt(agent, skills=[('GSTIN', 'Check the checksum.')])
        self.assertIn('Check the checksum.', prompt)


class SpendCapTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='A',             guardrails={'spendCapRupees': 100},
        )

    def _log(self, rupees):
        """A finished run that cost roughly `rupees`.

        Writes `tokens_used`, which is what a real run records. This used to
        write `credits_used` — a column no code path has ever set — so the
        whole class passed while the cap it was testing could not refuse
        anything in production.
        """
        from logs.models import ExecutionLog
        from workflow_backend.thresholds import RUPEES_PER_MILLION_TOKENS

        ExecutionLog.objects.create(
            subagent=self.agent, user=self.user, status='completed',
            tokens_used=rupees * 1_000_000 // RUPEES_PER_MILLION_TOKENS,
        )

    def test_the_cap_reads_the_column_runs_actually_write(self):
        """`credits_used` is written by nothing, so the cap must not read it."""
        from logs.models import ExecutionLog

        ExecutionLog.objects.create(subagent=self.agent, user=self.user,
                                    status='completed', credits_used=10_000,
                                    tokens_used=0)
        async_to_sync(check_guardrails)(self.agent, self.user)  # does not raise

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
        self.agent = SubAgent.objects.create(
            user=self.user, name='A',             prompt='Do the thing.', tool_grants={'rag': True}, guardrails={},
        )
        self.url = reverse('orchestrator:agent_execute', args=[self.agent.id])

    def test_a_run_is_accepted_and_returns_an_execution_id(self):
        """202, not 200 — the run now streams rather than blocking.

        Every tool call is broadcast to `ws/execution/{execution_id}/` so the
        canvas can draw it. Waiting here for the run to finish would
        make that stream useless: nobody can subscribe to an id they have not
        been handed yet.
        """
        async def fake_start(agent, goal, **kwargs):
            return 'e1'

        with patch('agents.agent.runtime.start_agent_run',
                   side_effect=fake_start):
            response = self.client.post(self.url, {'goal': 'go'}, format='json')

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data['execution_id'], 'e1')

    def test_unserved_grants_are_reported_when_the_run_starts(self):
        # Named so the caller can tell the user a configured capability was not
        # honoured, rather than leaving them to infer it. Available up front
        # because it is derived from the grants, not from the run.
        self.agent.tool_grants = {'rag': True, 'shell': True}
        self.agent.save(update_fields=['tool_grants'])

        async def fake_start(agent, goal, **kwargs):
            return 'e1'

        with patch('agents.agent.runtime.start_agent_run',
                   side_effect=fake_start):
            response = self.client.post(self.url, {'goal': 'go'}, format='json')

        self.assertEqual(response.data['unserved_grants'], ['shell'])

    def test_a_run_opens_and_closes_an_execution_log(self):
        """The ledger the `runs` / `unattended` / `spend` stats are counted from.

        Exercised against the real helpers rather than through the view: the
        point is that the run is recorded, and a fake that logged for us would
        be testing the fake.
        """
        from logs.models import ExecutionLog
        from agents.agent.runtime import _close_log, _open_log

        log = async_to_sync(_open_log)(self.agent, self.user, 'go', 'manual', 't-1')
        self.assertEqual(log.status, 'running')
        # The thread id is stored alongside the goal because resuming an
        # approved run has to find this same log — see `resume_agent_run`.
        self.assertEqual(log.input_data, {'goal': 'go', 'thread_id': 't-1'})

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

    def test_spend_cap_refusal_is_reported_as_payment_required(self):
        # Guardrails are checked before the 202, so a refusal reaches the caller
        # instead of killing a run that already looked like it started.
        async def refuse(*a, **k):
            raise AgentRunRefused('cap reached')

        with patch('agents.agent.runtime.start_agent_run', side_effect=refuse):
            response = self.client.post(self.url, {'goal': 'go'}, format='json')

        self.assertEqual(response.status_code, 402)
        self.assertIn('cap reached', response.data['error'])


class MissingCredentialTests(APITestCase):
    """An agent whose provider has no credential is refused, not started.

    The failure this locks out: `POST .../execute/` returning 202 with an
    execution id, and the run then dying in the background on its first model
    call. The user is handed a run that looks live, and the reason — "you have
    no OpenAI credential" — is buried in a failed execution they have to go and
    open. Nothing about that is discoverable, and every part of it is fixable
    before the run starts.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='nocred', password='pw')
        self.client.force_authenticate(user=self.user)
        # `openai` deliberately: the test settings ship a platform NVIDIA key,
        # so nvidia would resolve and prove nothing.
        self.agent = SubAgent.objects.create(
            user=self.user, name='A',             prompt='Do the thing.', tool_grants={}, guardrails={},
            llm_provider='openai', llm_model='gpt-4o-mini',
        )
        self.url = reverse('orchestrator:agent_execute', args=[self.agent.id])

    def test_execute_reports_the_missing_credential_instead_of_202(self):
        response = self.client.post(self.url, {'goal': 'go'}, format='json')

        self.assertEqual(response.status_code, 402)
        error = response.data['error']
        # The provider is named and the fix is stated — the two things that
        # make this actionable rather than just a failure.
        self.assertIn('OpenAI', error)
        self.assertIn('credential', error)
        self.assertIn('Settings', error)

    def test_no_execution_log_is_opened_for_a_run_that_cannot_start(self):
        """Refused before the log exists, so the stats stay honest.

        A run recorded as failed would count against the agent in `runs` and
        `unattended`, for a call that was never made.
        """
        from logs.models import ExecutionLog

        self.client.post(self.url, {'goal': 'go'}, format='json')
        self.assertFalse(ExecutionLog.objects.filter(subagent=self.agent).exists())

    def test_a_configured_provider_still_starts(self):
        """The guard is about a missing credential, not about agents in general."""
        self.agent.llm_provider = 'nvidia'   # platform key, see settings/test.py
        self.agent.save(update_fields=['llm_provider'])

        async def fake_start(agent, goal, **kwargs):
            return 'e1'

        with patch('agents.agent.runtime.start_agent_run',
                   side_effect=fake_start):
            response = self.client.post(self.url, {'goal': 'go'}, format='json')

        self.assertEqual(response.status_code, 202)

    def test_a_direct_run_fails_the_run_rather_than_the_first_model_call(self):
        """Schedules and triggers reach `run_agent` without passing the view.

        They get the same typed error, and because it is raised inside the
        run's own try the log is closed as failed with the message on it — not
        left `running` forever.
        """
        from llm.access import LLMNoCredential
        from logs.models import ExecutionLog
        from agents.agent.runtime import run_agent

        with self.assertRaises(LLMNoCredential):
            async_to_sync(run_agent)(self.agent, 'go', user=self.user,
                                     trigger_type='schedule')

        log = ExecutionLog.objects.get(subagent=self.agent)
        self.assertEqual(log.status, 'failed')
        self.assertIn('OpenAI', log.error_message)
