"""
Which connections an agent may reach, and how much of each — the second axis to
the `mcp` grant.

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

The 2026-09-03 widening added a third question inside the second: choosing a
mailbox used to grant sending and deleting along with reading, because there was
nothing finer than the connection to choose. `ConnectorScope` answers per tool,
and the tests below are in three groups accordingly — resolving the stored
shape, the dispatch door, and the descriptor door.
"""
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents import connector_scope
from agents.agent.runtime import AgentToolbox
from agents.models import SubAgent
from mcp_integration.models import MCPServer
from mcp_integration.tool_provider import encode_tool_name


def agent_with(connectors, **kwargs):
    """An unsaved SubAgent carrying a connector selection. No database needed.

    `connector_scope.for_agent` reads a JSON column and resolves nothing, which
    is the whole reason it is sync — so the tests for it need no rows.
    """
    return SubAgent(
        name='test', agent_context={'connectors': connectors},
        tool_grants={'mcp': True}, **kwargs,
    )


def scope_for(connectors):
    return connector_scope.for_agent(agent_with(connectors))


class ScopeResolutionTests(SimpleTestCase):
    """`connector_scope.for_agent`: a stored list becomes a scope, or None."""

    def test_no_selection_is_unrestricted(self):
        self.assertIsNone(scope_for([]))
        self.assertIsNone(connector_scope.for_agent(SubAgent(name='t', agent_context={})))
        self.assertIsNone(connector_scope.for_agent(SubAgent(name='t', agent_context=None)))

    def test_a_selection_becomes_a_set_of_ids(self):
        self.assertEqual(scope_for([7, 3, 3]).server_ids, frozenset({3, 7}))

    def test_a_bare_id_is_the_legacy_shape_and_means_everything(self):
        """No migration: the field held plain integers until 2026-09-03, and an
        agent that never chose a mode must not narrow when the feature lands."""
        scope = scope_for([7])
        self.assertTrue(scope.tool_allowed(7, 'send_message'))
        self.assertTrue(scope.tool_allowed(7, 'delete_everything'))

    def test_read_mode_is_derived_not_stored(self):
        """The whole reason mode beats a stored name list: a tool added to the
        server next week is judged by the same rule as one added last year."""
        scope = scope_for([{'id': 7, 'mode': 'read'}])
        self.assertTrue(scope.tool_allowed(7, 'search_threads'))
        self.assertTrue(scope.tool_allowed(7, 'get_message'))
        self.assertFalse(scope.tool_allowed(7, 'send_message'))
        self.assertFalse(scope.tool_allowed(7, 'trash_thread'))

    def test_selected_names_only_what_it_names(self):
        scope = scope_for([{'id': 7, 'mode': 'selected',
                            'tools': ['search_threads', 'create_draft']}])
        self.assertTrue(scope.tool_allowed(7, 'create_draft'))
        self.assertFalse(scope.tool_allowed(7, 'send_message'))
        # A tool that appears on the server later is not in the set, and is not
        # added to it: the user picked a set and additions were not in it.
        self.assertFalse(scope.tool_allowed(7, 'brand_new_tool'))

    def test_names_are_compared_the_way_the_encoder_writes_them(self):
        """Both sides go through the encoder's own sanitiser plus case folding.

        Not more than that: `_SAFE_TOOL_NAME_RE` keeps hyphens, so folding `-`
        into `_` here would make two tools that differ only by separator into
        one — a widening, when the user picked one of them.
        """
        scope = scope_for([{'id': 7, 'mode': 'selected', 'tools': ['Send-Email']}])
        self.assertTrue(scope.tool_allowed(7, 'send-email'))
        self.assertTrue(scope.tool_allowed(7, 'SEND-EMAIL'))
        self.assertFalse(scope.tool_allowed(7, 'send_email'))

    def test_selected_with_no_names_stays_open(self):
        """A connection with no usable tools is not something a user can have
        meant; it reads as the picking not being finished."""
        scope = scope_for([{'id': 7, 'mode': 'selected', 'tools': []}])
        self.assertTrue(scope.tool_allowed(7, 'send_message'))

    def test_an_unknown_mode_is_not_a_wider_one(self):
        scope = scope_for([{'id': 7, 'mode': 'whatever'}])
        self.assertEqual(scope.modes[7], 'all')

    def test_the_two_shapes_mix(self):
        scope = scope_for([3, {'id': 7, 'mode': 'read'}])
        self.assertEqual(scope.server_ids, frozenset({3, 7}))
        self.assertTrue(scope.tool_allowed(3, 'send_message'))
        self.assertFalse(scope.tool_allowed(7, 'send_message'))

    def test_legacy_slugs_read_as_unrestricted_not_as_nothing(self):
        """The field held presentation slugs before it was enforced.

        Migration `0021` clears them, but a row it does not reach — a fixture
        loaded later, a restored dump — must degrade to the behaviour it has
        always had. Reading `['gmail']` as a scope of zero servers would leave
        an agent with no connectors and nothing in the UI to explain why.
        """
        self.assertIsNone(scope_for(['gmail', 'sheets']))

    def test_a_mixed_row_keeps_only_the_ids(self):
        self.assertEqual(scope_for(['gmail', 4]).server_ids, frozenset({4}))

    def test_a_boolean_is_not_a_server_id(self):
        # `True == 1` in Python, so a stray boolean would otherwise silently
        # scope an agent to whichever server happens to have id 1.
        self.assertIsNone(scope_for([True]))


class DispatchGateTests(SimpleTestCase):
    """The door that matters: a tool the model named but was never offered."""

    def box(self, connectors):
        return AgentToolbox(
            grants={'mcp': True}, user_id=1,
            mcp_scope=None if connectors is None else connector_scope.parse(connectors),
        )

    def allowed(self, box, server_id, tool):
        return box.mcp_call_allowed(encode_tool_name(server_id, tool))[0]

    def test_unrestricted_allows_any_connection(self):
        self.assertTrue(self.allowed(self.box(None), 9, 'send_message'))

    def test_a_selected_connection_is_allowed(self):
        self.assertTrue(self.allowed(self.box([3, 7]), 7, 'gmail_search'))

    def test_a_tool_outside_the_mode_is_refused_at_dispatch(self):
        """The door that matters for the tool axis too. Withholding the
        descriptor is presentation; a model names tools it saw earlier."""
        box = self.box([{'id': 7, 'mode': 'read'}])
        self.assertTrue(self.allowed(box, 7, 'search_threads'))
        self.assertFalse(self.allowed(box, 7, 'send_message'))

    def test_the_refusal_says_which_boundary_was_hit(self):
        """A model told only "denied" retries the same call until the iteration
        cap ends the run."""
        box = self.box([{'id': 7, 'mode': 'read'}])
        _, reason = box.mcp_call_allowed(encode_tool_name(7, 'send_message'))
        self.assertIn('only read', reason)
        _, reason = box.mcp_call_allowed(encode_tool_name(9, 'send_message'))
        self.assertEqual(reason, 'that connection')

    def test_the_digest_suffix_does_not_defeat_the_match(self):
        """`encode_tool_name` appends 8 hex characters so two tools that
        sanitise alike stay distinct; the scope has to undo exactly that much."""
        box = self.box([{'id': 7, 'mode': 'selected', 'tools': ['send_message']}])
        self.assertTrue(self.allowed(box, 7, 'send_message'))
        self.assertFalse(self.allowed(box, 7, 'send_message_now'))

    def test_an_unselected_connection_is_refused(self):
        """The bug this whole feature exists for.

        An agent given Gmail must not reach Slack because both happen to be
        connected to the same account.
        """
        self.assertFalse(self.allowed(self.box([3, 7]), 9, 'slack_post'))

    def test_an_unparseable_mcp_name_is_refused(self):
        # `is_mcp_tool` only checks the prefix, so a name carrying no server id
        # reaches here. Allowing it would make `mcp__` alone enough to escape
        # the selection.
        self.assertFalse(self.box([3]).mcp_call_allowed('mcp__not_a_number')[0])

    def test_dispatch_refuses_an_unselected_connection(self):
        box = self.box([3])
        result = async_to_sync(box.dispatch)(
            encode_tool_name(9, 'slack_post'), {}, {},
        )
        self.assertIn('not available to this agent', result)

    def test_dispatch_still_refuses_everything_without_the_grant(self):
        """The selection is the second axis, never a replacement for the grant."""
        box = AgentToolbox(grants={'mcp': False}, user_id=1,
                           mcp_scope=connector_scope.parse([3]))
        result = async_to_sync(box.dispatch)(encode_tool_name(3, 'x'), {}, {})
        self.assertIn('not available to this agent', result)

    def test_plan_withholds_every_connection_however_narrow_the_scope(self):
        """`read_only` is a wider refusal and must win over a valid selection."""
        box = AgentToolbox(grants={'mcp': True}, user_id=1,
                           mcp_scope=connector_scope.parse([3]), read_only=True)
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

    def _descriptors(self, server_ids, tool_filter=None):
        """Descriptors, with `list_tools` stubbed so no subprocess is spawned.

        The unit under test is which servers are consulted, not what they
        answer, so each server reports one tool named after itself.
        """
        from unittest.mock import patch

        from mcp_integration.tool_provider import MCPToolProvider

        async def fake_list_tools(self):
            return [
                {'name': f'tool_{self.server_id}', 'inputSchema': {'type': 'object'}},
                {'name': 'send_message', 'inputSchema': {'type': 'object'}},
                {'name': 'list_messages', 'inputSchema': {'type': 'object'}},
            ]

        with patch('mcp_integration.client.MCPClientManager.list_tools',
                   fake_list_tools):
            return async_to_sync(MCPToolProvider.get_openai_tool_descriptors)(
                self.user.id, server_ids, tool_filter,
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
        self.assertTrue(got)
        for descriptor in got:
            self.assertIn(f'mcp__{self.a.id}__', descriptor['function']['name'])

    def test_read_mode_withholds_the_writing_tools(self):
        """The filter runs where the *original* names are, which is the only
        place they exist — everything downstream sees the encoded form."""
        scope = connector_scope.parse([{'id': self.a.id, 'mode': 'read'}])
        names = {
            d['function']['name'] for d in
            self._descriptors([self.a.id], scope.tool_allowed)
        }
        self.assertTrue(any('list_messages' in n for n in names))
        self.assertFalse(any('send_message' in n for n in names))

    def test_selected_mode_offers_only_the_named_tools(self):
        scope = connector_scope.parse([
            {'id': self.a.id, 'mode': 'selected', 'tools': ['send_message']},
        ])
        names = {
            d['function']['name'] for d in
            self._descriptors([self.a.id], scope.tool_allowed)
        }
        self.assertEqual(len(names), 1)
        self.assertTrue(any('send_message' in n for n in names))

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
