"""
Which connections an agent may reach — the second axis to the `mcp` grant.

Grouped by the mistake each test exists to catch. The one they descend from is
the mistake this feature fixes: `mcp` was a boolean, and an agent that held it
was handed every connector the *account* owned. An inbox-triage agent could
therefore send Slack messages and write Notion pages, and the autonomy ladder
could not help — it decides whether a call is *paused*, never whether the tool
should have been in the toolbox at all.

The two properties that carry it:

  * **Withholding a descriptor is not access control.** A model names tools it
    saw in an earlier turn, so `dispatch` re-checks. That is why the enforcement
    is tested twice, once at each door.
  * **Empty means unrestricted.** The selection existed in the builder long
    before anything read it, so enforcing it must not silently empty the toolbox
    of every agent that never made a choice.
"""
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents.agent.runtime import AgentToolbox, mcp_scope_for
from agents.models import SubAgent
from mcp_integration.models import MCPServer
from mcp_integration.tool_provider import encode_tool_name


def agent_with(connectors, **kwargs):
    """An unsaved SubAgent carrying a connector selection. No database needed.

    `mcp_scope_for` reads a JSON column and resolves nothing, which is the whole
    reason it is sync — so the tests for it need no rows.
    """
    return SubAgent(
        name='test', agent_context={'connectors': connectors},
        tool_grants={'mcp': True}, **kwargs,
    )


class ScopeResolutionTests(SimpleTestCase):
    """`mcp_scope_for`: a stored list becomes a scope, or None."""

    def test_no_selection_is_unrestricted(self):
        self.assertIsNone(mcp_scope_for(agent_with([])))
        self.assertIsNone(mcp_scope_for(SubAgent(name='t', agent_context={})))
        self.assertIsNone(mcp_scope_for(SubAgent(name='t', agent_context=None)))

    def test_a_selection_becomes_a_sorted_tuple(self):
        self.assertEqual(mcp_scope_for(agent_with([7, 3, 3])), (3, 7))

    def test_legacy_slugs_read_as_unrestricted_not_as_nothing(self):
        """The field held presentation slugs before it was enforced.

        Migration `0021` clears them, but a row it does not reach — a fixture
        loaded later, a restored dump — must degrade to the behaviour it has
        always had. Reading `['gmail']` as a scope of zero servers would leave
        an agent with no connectors and nothing in the UI to explain why.
        """
        self.assertIsNone(mcp_scope_for(agent_with(['gmail', 'sheets'])))

    def test_a_mixed_row_keeps_only_the_ids(self):
        self.assertEqual(mcp_scope_for(agent_with(['gmail', 4])), (4,))

    def test_a_boolean_is_not_a_server_id(self):
        # `True == 1` in Python, so a stray boolean would otherwise silently
        # scope an agent to whichever server happens to have id 1.
        self.assertIsNone(mcp_scope_for(agent_with([True])))


class DispatchGateTests(SimpleTestCase):
    """The door that matters: a tool the model named but was never offered."""

    def box(self, servers):
        return AgentToolbox(grants={'mcp': True}, user_id=1, mcp_servers=servers)

    def test_unrestricted_allows_any_connection(self):
        box = self.box(None)
        self.assertTrue(box.mcp_server_allowed(encode_tool_name(9, 'send_message')))

    def test_a_selected_connection_is_allowed(self):
        box = self.box((3, 7))
        self.assertTrue(box.mcp_server_allowed(encode_tool_name(7, 'gmail_search')))

    def test_an_unselected_connection_is_refused(self):
        """The bug this whole feature exists for.

        An agent given Gmail must not reach Slack because both happen to be
        connected to the same account.
        """
        box = self.box((3, 7))
        self.assertFalse(box.mcp_server_allowed(encode_tool_name(9, 'slack_post')))

    def test_an_unparseable_mcp_name_is_refused(self):
        # `is_mcp_tool` only checks the prefix, so a name carrying no server id
        # reaches here. Allowing it would make `mcp__` alone enough to escape
        # the selection.
        self.assertFalse(self.box((3,)).mcp_server_allowed('mcp__not_a_number'))

    def test_dispatch_refuses_an_unselected_connection(self):
        box = self.box((3,))
        result = async_to_sync(box.dispatch)(
            encode_tool_name(9, 'slack_post'), {}, {},
        )
        self.assertIn('not available to this agent', result)

    def test_dispatch_still_refuses_everything_without_the_grant(self):
        """The selection is the second axis, never a replacement for the grant."""
        box = AgentToolbox(grants={'mcp': False}, user_id=1, mcp_servers=(3,))
        result = async_to_sync(box.dispatch)(encode_tool_name(3, 'x'), {}, {})
        self.assertIn('not available to this agent', result)

    def test_plan_withholds_every_connection_however_narrow_the_scope(self):
        """`read_only` is a wider refusal and must win over a valid selection."""
        box = AgentToolbox(grants={'mcp': True}, user_id=1, mcp_servers=(3,),
                           read_only=True)
        self.assertFalse(box.mcp_allowed)
        result = async_to_sync(box.dispatch)(encode_tool_name(3, 'x'), {}, {})
        self.assertIn('not available to this agent', result)


class ProviderFilterTests(TestCase):
    """The other door: what the model is offered in the first place."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.a = MCPServer.objects.create(
            user=self.user, name='A', type='stdio', command='npx', enabled=True,
        )
        self.b = MCPServer.objects.create(
            user=self.user, name='B', type='stdio', command='npx', enabled=True,
        )

    def _descriptors(self, server_ids):
        """Descriptors, with `list_tools` stubbed so no subprocess is spawned.

        The unit under test is which servers are consulted, not what they
        answer, so each server reports one tool named after itself.
        """
        from unittest.mock import patch

        from mcp_integration.tool_provider import MCPToolProvider

        async def fake_list_tools(self):
            return [{'name': f'tool_{self.server_id}', 'inputSchema': {'type': 'object'}}]

        with patch('mcp_integration.client.MCPClientManager.list_tools',
                   fake_list_tools):
            return async_to_sync(MCPToolProvider.get_openai_tool_descriptors)(
                self.user.id, server_ids,
            )

    def test_none_consults_every_visible_server(self):
        """Including the curated catalogue, which migrations seed for everyone.

        Asserted as "both of mine are in there" rather than as a count: the
        curated rows are real and visible, so a count would pin this test to the
        size of a catalogue that has changed four times already.
        """
        names = {d['function']['name'] for d in self._descriptors(None)}
        self.assertTrue(any(f'mcp__{self.a.id}__' in n for n in names))
        self.assertTrue(any(f'mcp__{self.b.id}__' in n for n in names))

    def test_a_selection_consults_only_what_it_names(self):
        got = self._descriptors([self.a.id])
        self.assertEqual(len(got), 1)
        self.assertIn(f'mcp__{self.a.id}__', got[0]['function']['name'])

    def test_a_stale_id_can_only_take_tools_away(self):
        """A selection is intersected with what the user can see, never trusted.

        An agent may name a connection that was later deleted, or one the user
        switched off on the Connections page. That must yield fewer tools, never
        a server resolved because an agent's config still remembers it.
        """
        self.assertEqual(self._descriptors([9_999_999]), [])

    def test_an_empty_selection_offers_nothing(self):
        # Distinct from None: the runtime turns "no choice" into None before it
        # gets here, so an empty list arriving means a caller asked for nothing.
        self.assertEqual(self._descriptors([]), [])
