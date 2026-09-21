"""
Per-tool allow/ask/deny on a subagent — the fourth axis after grant (whether),
scope (which rows) and `toolScope` (which tools): *how* each built-in tool may
be used.

Grouped by the mistake each test exists to catch. The one they descend from is
the gap this feature closes: an agent holding `fileOps` could read, write and
delete alike, and the only dial was the whole agent's autonomy level — so
freeing one noisy tool (`write_file` under `auto`) meant freeing everything,
and tightening one dangerous tool meant interrogating the user about all of them.

The properties that carry it:

  * **A rule names only what a grant can unlock.** MCP and native connector
    names are minted at runtime and would rot; the plan, the clock and the
    archive are infrastructure, not capabilities. All are refused on save.
  * **Empty means today's behaviour.** The field arrives after the agents, so
    an agent that never chose keeps running exactly as it did.
  * **Enforcement is tested at both doors.** Withholding a descriptor is not
    access control — `dispatch` re-checks — and the approval overlay is
    tested as the pure function `tools_node` actually calls.
  * **A worker never widens its parent.** Restrictions flow down
    most-restrictive-wins, or delegation becomes a way around your own rules.
"""
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents.agent.runtime import (
    AgentToolbox,
    merge_tool_permissions,
    tool_permissions_for,
)
from agents.models import SubAgent
from chat.tools import permissions


#: Stands in for an `inference.vfs.FileScope`, as in `test_autonomy.py`: the
#: toolbox only asks whether it is None, so any object will do.
FILE_SCOPE = object()


def agent_with(tool_permissions=None, **grants):
    """An unsaved SubAgent carrying a per-tool map. No database needed."""
    return SubAgent(
        name='test',
        agent_context={'toolPermissions': tool_permissions or {}},
        tool_grants={'webSearch': True, 'fileOps': True, **grants},
    )


def box_for(agent):
    return AgentToolbox.for_agent(agent, user_id=1, file_scope=FILE_SCOPE)


class ResolutionTests(SimpleTestCase):
    """`tool_permissions_for`: the stored map becomes a clean one, or `{}`."""

    def test_absent_is_empty(self):
        self.assertEqual(tool_permissions_for(agent_with()), {})
        self.assertEqual(
            tool_permissions_for(SubAgent(name='t', agent_context={})), {})
        self.assertEqual(
            tool_permissions_for(SubAgent(name='t', agent_context=None)), {})

    def test_a_non_dict_is_empty_not_a_500(self):
        agent = agent_with()
        agent.agent_context = {'toolPermissions': ['deny-everything']}
        self.assertEqual(tool_permissions_for(agent), {})

    def test_modes_are_lowercased_and_kept(self):
        agent = agent_with({'write_file': 'ASK', 'read_file': 'allow'})
        self.assertEqual(tool_permissions_for(agent),
                         {'write_file': 'ask', 'read_file': 'allow'})

    def test_an_unknown_mode_is_dropped_not_stored(self):
        # A stricter reading would refuse the run; a looser one would apply
        # something. Dropping leaves today's behaviour, which is the only
        # reading that cannot surprise either way.
        agent = agent_with({'write_file': 'sometimes'})
        self.assertEqual(tool_permissions_for(agent), {})

    def test_connector_and_infra_names_are_dropped(self):
        # Built-ins only: an MCP name would rot on rename, and the plan, the
        # clock and the archive are not capabilities. The serializer refuses
        # these on save; a stale or hand-edited row must still run safely.
        agent = agent_with({
            'mcp__7__send_email_ab12cd34': 'deny',
            'gmail_search_threads': 'ask',
            'update_todos': 'deny',
            'read_tool_output': 'ask',
            'write_file': 'deny',
        })
        self.assertEqual(tool_permissions_for(agent), {'write_file': 'deny'})


class MergeTests(SimpleTestCase):
    """`merge_tool_permissions`: most restrictive wins, either direction."""

    def test_empty_maps_merge_to_empty(self):
        self.assertEqual(merge_tool_permissions({}, {}), {})

    def test_a_parent_deny_beats_a_worker_allow(self):
        self.assertEqual(
            merge_tool_permissions({'delete_file': 'deny'},
                                   {'delete_file': 'allow'}),
            {'delete_file': 'deny'})

    def test_a_parent_ask_narrows_a_worker_allow(self):
        self.assertEqual(
            merge_tool_permissions({'write_file': 'ask'}, {'write_file': 'allow'}),
            {'write_file': 'ask'})

    def test_a_worker_deny_beats_a_parent_ask(self):
        # Narrowing your own worker further is always allowed.
        self.assertEqual(
            merge_tool_permissions({'write_file': 'ask'}, {'write_file': 'deny'}),
            {'write_file': 'deny'})

    def test_unset_on_either_side_does_not_widen_the_other(self):
        self.assertEqual(
            merge_tool_permissions({'write_file': 'ask'}, {}),
            {'write_file': 'ask'})
        self.assertEqual(
            merge_tool_permissions({}, {'write_file': 'deny'}),
            {'write_file': 'deny'})

    def test_disjoint_maps_union(self):
        self.assertEqual(
            merge_tool_permissions({'a': 'ask'}, {'b': 'deny'}),
            {'a': 'ask', 'b': 'deny'})


class OfferDoorTests(SimpleTestCase):
    """`allowed_names`: deny withholds, ask and allow keep offering."""

    def test_deny_withholds_the_tool(self):
        box = box_for(agent_with({'write_file': 'deny'}))
        self.assertNotIn('write_file', box.allowed_names)
        # …while its siblings stay offered.
        self.assertIn('read_file', box.allowed_names)

    def test_ask_and_allow_keep_the_tool_offered(self):
        box = box_for(agent_with({'write_file': 'ask', 'read_file': 'allow'}))
        self.assertIn('write_file', box.allowed_names)
        self.assertIn('read_file', box.allowed_names)

    def test_deny_on_an_ungranted_tool_changes_nothing(self):
        box = box_for(agent_with({'generate_image': 'deny'}))
        self.assertNotIn('generate_image', box.allowed_names)

    def test_allow_cannot_resurrect_an_ungranted_tool(self):
        # The grant decides reachability and wins: `allow` moves the approval
        # gate, never the offer set.
        box = box_for(agent_with({'generate_image': 'allow'}))
        self.assertNotIn('generate_image', box.allowed_names)

    def test_empty_map_offers_everything_the_grants_unlock(self):
        self.assertEqual(box_for(agent_with()).allowed_names,
                         box_for(agent_with({})).allowed_names)


class DispatchDoorTests(SimpleTestCase):
    """`dispatch`: a denied tool is refused even when named directly."""

    def test_a_denied_tool_is_refused_with_its_own_reason(self):
        box = box_for(agent_with({'write_file': 'deny'}))
        out = async_to_sync(box.dispatch)('write_file', {}, {})
        self.assertIn('deny', out)
        # …and the reason names the rule, not the grant: the grant *is*
        # held, so "was not granted" would send the owner to flip a switch
        # that changes nothing.
        self.assertNotIn('was not granted', out)

    def test_ask_and_allow_pass_through_to_the_tool(self):
        # They are approval questions, not dispatch ones: the call must reach
        # the tool. `execute_tool` is stubbed — the point is the permission
        # check lets it through, not what a real search returns.
        from unittest import mock

        async def _ran(name, args, context):
            return 'ran'

        for mode in ('ask', 'allow'):
            box = box_for(agent_with({'web_search': mode}))
            with mock.patch('chat.tools.execute_tool', _ran):
                out = async_to_sync(box.dispatch)(
                    'web_search', {'query': 'x'}, {'user_id': 1})
            self.assertEqual(out, 'ran', mode)


class ApprovalOverlayTests(SimpleTestCase):
    """`apply_tool_permission_overrides`: the pure half of Pass 1."""

    def test_empty_leaves_the_ladder_alone(self):
        sensitive = frozenset({'write_file'})
        self.assertEqual(
            permissions.apply_tool_permission_overrides(sensitive, {}),
            sensitive)
        self.assertEqual(
            permissions.apply_tool_permission_overrides(sensitive, None),
            sensitive)

    def test_ask_adds_even_what_the_level_freed(self):
        # `auto` frees recoverable writes; a per-tool ask takes one back.
        self.assertIn(
            'write_file',
            permissions.apply_tool_permission_overrides(
                frozenset(), {'write_file': 'ask'}))

    def test_allow_removes_even_what_the_level_gated(self):
        # `ask` gates file writes; a per-tool allow frees one.
        self.assertNotIn(
            'write_file',
            permissions.apply_tool_permission_overrides(
                frozenset({'write_file', 'execute_python'}),
                {'write_file': 'allow'}))

    def test_deny_is_not_a_gate(self):
        # Denied tools never reach the approval pass — withheld and refused
        # by the toolbox — so the overlay must ignore the mode entirely.
        sensitive = frozenset({'write_file'})
        self.assertEqual(
            permissions.apply_tool_permission_overrides(
                sensitive, {'write_file': 'deny'}),
            sensitive)
        self.assertEqual(
            permissions.apply_tool_permission_overrides(
                frozenset(), {'delete_file': 'deny'}),
            frozenset())

    def test_unknown_modes_change_nothing(self):
        sensitive = frozenset({'write_file'})
        self.assertEqual(
            permissions.apply_tool_permission_overrides(
                sensitive, {'write_file': 'sometimes'}),
            sensitive)


class ToolContextCarryTests(SimpleTestCase):
    """The map reaches the tool context `invoke_subagent` reads."""

    def test_turn_context_defaults_to_empty(self):
        from chat.turn.agent import TurnContext

        turn = TurnContext(provider='p', model='m', system_message='s',
                           user_id=1, session_id='s', intent='q', user_text='t')
        self.assertEqual(turn.tool_permissions, {})

    def test_merge_is_what_a_worker_is_built_from(self):
        # The contract `invoke_subagent` implements: parent map (as carried
        # on the context) merged most-restrictive-wins with the worker row.
        parent = {'delete_file': 'deny', 'write_file': 'ask'}
        worker = tool_permissions_for(agent_with({'write_file': 'allow'}))
        self.assertEqual(merge_tool_permissions(parent, worker), {
            'delete_file': 'deny', 'write_file': 'ask'})


class ShapeApiTests(TestCase):
    """The wire shape, where the user meets it."""

    def setUp(self):
        from rest_framework.test import APIClient

        self.user = User.objects.create_user(username='perm', password='pw')
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def _post(self, payload):
        body = {'name': 'A', 'tools': {'fileOps': True, 'webSearch': True}}
        body.update(payload)
        return self.client.post('/api/orchestrator/agents/', body, format='json')

    def test_a_map_round_trips(self):
        from django.urls import reverse

        response = self._post(
            {'toolPermissions': {'write_file': 'ask', 'delete_file': 'deny'}})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['toolPermissions'],
                         {'write_file': 'ask', 'delete_file': 'deny'})
        url = reverse('orchestrator:agent_detail', args=[response.data['id']])
        got = self.client.get(url)
        self.assertEqual(got.data['toolPermissions'],
                         {'write_file': 'ask', 'delete_file': 'deny'})

    def test_absent_reads_back_empty(self):
        response = self._post({})
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['toolPermissions'], {})

    def test_an_unknown_tool_is_refused(self):
        response = self._post({'toolPermissions': {'no_such_tool': 'deny'}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('toolPermissions', response.data)

    def test_a_bad_mode_is_refused(self):
        response = self._post({'toolPermissions': {'write_file': 'sometimes'}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('toolPermissions', response.data)

    def test_infra_tools_are_refused(self):
        # The plan, the clock and the archive are not capabilities.
        for name in ('update_todos', 'render_chart', 'read_tool_output',
                     'recall_context', 'get_current_time'):
            response = self._post({'toolPermissions': {name: 'deny'}})
            self.assertEqual(response.status_code, 400, name)
            self.assertIn('toolPermissions', response.data)

    def test_a_patch_merges_and_a_reread_agrees(self):
        from django.urls import reverse

        created = self._post({'toolPermissions': {'write_file': 'ask'}})
        url = reverse('orchestrator:agent_detail', args=[created.data['id']])
        patched = self.client.patch(
            url, {'toolPermissions': {'write_file': 'allow'}}, format='json')
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data['toolPermissions'],
                         {'write_file': 'allow'})
        got = self.client.get(url)
        self.assertEqual(got.data['toolPermissions'], {'write_file': 'allow'})
