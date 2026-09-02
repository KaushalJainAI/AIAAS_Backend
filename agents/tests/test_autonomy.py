"""
The autonomy ladder: what each level gates, and how it can be changed mid-run.

Grouped by the mistake each test exists to catch, in the style of
`test_regressions.py`, because the failure modes here are not module-shaped.
The one they all descend from is the same: a permissions screen that says one
thing while the runtime does another. Adding a *level* multiplies the ways
those two can disagree, so every level is pinned against the runtime rather
than against the serializer that offers it.
"""
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from agents.agent.runtime import (
    AUTONOMY_LADDER,
    AgentToolbox,
    approval_policy_for,
    sensitive_tools_for,
    switchable_modes,
)
from agents.models import SubAgent
from chat.tools import READ_ONLY_TOOLS, SENSITIVE_TOOLS, all_tools, effect_of
from chat.turn import steering


#: Stands in for an `inference.vfs.FileScope`. The toolbox only ever asks
#: whether it is None — with no scope the file tools are withheld whatever the
#: grant says — so any object will do, and building a real one would drag in
#: the database for a question about sets.
FILE_SCOPE = object()


def toolbox(read_only=False, file_scope=FILE_SCOPE, **grants) -> AgentToolbox:
    from agents.views.agents import TOOL_KEYS

    full = {k: bool(grants.get(k, False)) for k in TOOL_KEYS}
    return AgentToolbox(grants=full, user_id=1, file_scope=file_scope,
                        read_only=read_only)


class EffectDeclarationTests(SimpleTestCase):
    """`effect` is what the middle rungs of the ladder are computed from."""

    def test_every_registered_tool_declares_a_known_effect(self):
        for entry in all_tools():
            self.assertIn(
                entry.effect, ('read', 'reversible', 'irreversible'),
                f'{entry.name} declares an effect nothing knows how to gate',
            )

    def test_an_unregistered_name_is_irreversible(self):
        # Every MCP tool lands here — its name is minted at runtime from a
        # third-party catalogue and can never carry a declaration. If the
        # default were 'read', `auto` would quietly stop gating exactly the
        # calls that run with the user's real credentials.
        self.assertEqual(effect_of('mcp__7__send_email_ab12cd34'), 'irreversible')
        self.assertEqual(effect_of('no_such_tool'), 'irreversible')

    def test_read_only_set_is_not_the_complement_of_sensitive(self):
        # The two answer different questions and are both needed. If they were
        # the same set, `effect` would be a second spelling of `sensitive` and
        # `auto` could not exist.
        self.assertIn('execute_python', READ_ONLY_TOOLS)
        self.assertNotIn('execute_python', SENSITIVE_TOOLS)
        self.assertIn('write_file', SENSITIVE_TOOLS)
        self.assertNotIn('write_file', READ_ONLY_TOOLS)

    def test_recycled_file_writes_are_reversible_not_irreversible(self):
        # This is the distinction `auto` is built on: a delete goes through
        # `recycle.trash` into the user's own recycle bin, so it is a side
        # effect they can undo without our help.
        for name in ('write_file', 'make_directory', 'delete_file'):
            self.assertEqual(effect_of(name), 'reversible', name)

    def test_delegation_is_irreversible(self):
        # Spawning a run spends money and cannot be recalled, so `auto` must
        # still stop on it.
        self.assertEqual(effect_of('invoke_subagent'), 'irreversible')
        self.assertEqual(effect_of('run_agent'), 'irreversible')


class LadderTests(SimpleTestCase):
    """Each level gates strictly less than the one before it."""

    def setUp(self):
        self.box = toolbox(webSearch=True, fileOps=True, codeExecution=True,
                           subAgents=True)

    def test_the_ladder_is_monotonic(self):
        # The point of a ladder is that moving down it never *adds* a prompt.
        # A level that gated something a stricter level let through would make
        # "less strict" meaningless, and no UI could order the options.
        #
        # Compared against what the agent can actually call. The levels are not
        # scoped alike — `review` is built from this toolbox while `ask` and
        # `auto` name tools globally — and that is deliberate: gating a tool the
        # agent was never granted is a no-op, so narrowing them would be work
        # that changes no behaviour. What has to hold is that the *effective*
        # gate shrinks, which is the only version a user experiences.
        reachable = self.box.allowed_names
        gated = [
            sensitive_tools_for(level, self.box) & reachable
            for level in AUTONOMY_LADDER
        ]
        for stricter, looser, sname, lname in zip(
            gated, gated[1:], AUTONOMY_LADDER, AUTONOMY_LADDER[1:]
        ):
            # `plan` gates nothing because it withholds instead; skip the pair
            # where the stricter side is that special case.
            if sname == 'plan':
                continue
            self.assertTrue(
                looser <= stricter,
                f'{lname} gates {sorted(looser - stricter)} that {sname} does not',
            )

    def test_review_gates_everything_the_agent_has(self):
        gated = sensitive_tools_for('review', self.box)
        self.assertTrue(self.box.allowed_names <= gated)

    def test_full_gates_nothing(self):
        self.assertEqual(sensitive_tools_for('full', self.box), frozenset())

    def test_auto_gates_the_irreversible_and_frees_the_recoverable(self):
        gated = sensitive_tools_for('auto', self.box)
        self.assertIn('invoke_subagent', gated)
        # The whole point of the rung: a file write the user can undo stops
        # interrupting them, while delegation still does not.
        self.assertNotIn('write_file', gated)
        self.assertNotIn('delete_file', gated)
        self.assertNotIn('execute_python', gated)

    def test_ask_still_gates_file_writes(self):
        # `auto` exists precisely because `ask` does this. If `ask` ever stops,
        # the two levels have collapsed into one.
        gated = sensitive_tools_for('ask', self.box)
        self.assertIn('write_file', gated)
        self.assertIn('execute_python', gated)


class PlanModeTests(SimpleTestCase):
    """`plan` withholds tools rather than gating them."""

    def test_plan_withholds_everything_that_could_mutate(self):
        box = toolbox(read_only=True, webSearch=True, fileOps=True,
                      codeExecution=True, subAgents=True)
        names = box.allowed_names
        for withheld in ('write_file', 'make_directory', 'delete_file',
                         'invoke_subagent'):
            self.assertNotIn(withheld, names)
        # …while leaving it able to actually look at things, or it would be a
        # mode that can do nothing at all.
        self.assertIn('web_search', names)
        self.assertIn('read_file', names)

    def test_plan_withdraws_mcp_wholesale(self):
        # An MCP tool's name is minted at runtime, so nothing here can tell a
        # read from a write on that server. A mode promising nothing will
        # change cannot offer tools it cannot classify.
        box = toolbox(read_only=True, mcp=True)
        self.assertFalse(box.mcp_allowed)
        self.assertTrue(toolbox(mcp=True).mcp_allowed)

    def test_plan_gates_nothing_because_nothing_it_offers_mutates(self):
        box = toolbox(read_only=True, webSearch=True, fileOps=True)
        self.assertEqual(sensitive_tools_for('plan', box), frozenset())

    def test_plan_refuses_a_call_it_never_advertised(self):
        # Advertising is not access control: the model can name a tool it was
        # never offered, and `plan` has to refuse it at dispatch too.
        box = toolbox(read_only=True, fileOps=True)
        out = async_to_sync(box.dispatch)('write_file', {}, {})
        self.assertIn('not', out.lower())


class PolicyTests(SimpleTestCase):
    """The half of the gate that names cannot cover: MCP."""

    def test_unattended_levels_gate_credentialed_reads(self):
        from chat.tools import permissions

        self.assertIs(approval_policy_for('ask'), permissions.unattended_policy)
        self.assertIs(approval_policy_for('review'), permissions.unattended_policy)

    def test_auto_restores_the_read_exemption(self):
        # `auto` means "stop asking about things I can undo", and a read
        # changes nothing at all — so this is the level where a credentialed
        # read goes through while a credentialed write still stops.
        from chat.tools import permissions

        self.assertIs(approval_policy_for('auto'), permissions.default_policy)

    def test_plan_and_full_gate_nothing(self):
        from chat.tools import permissions

        self.assertIs(approval_policy_for('plan'), permissions.never)
        self.assertIs(approval_policy_for('full'), permissions.never)


class MidRunSwitchTests(SimpleTestCase):
    """Changing how much a run asks, while it is going."""

    def setUp(self):
        steering.clear()

    def tearDown(self):
        steering.clear()

    def test_a_level_is_not_drained_when_read(self):
        # The difference between a mode and a steer. A steer is an instruction
        # to act on once; a mode that evaporated after the next tool call would
        # have the user approving again seconds after saying "stop asking".
        steering.set_autonomy('t1', 'auto')
        self.assertEqual(steering.autonomy('t1'), 'auto')
        self.assertEqual(steering.autonomy('t1'), 'auto')

    def test_a_mode_and_a_steer_share_a_slot_without_clobbering(self):
        steering.post('t1', 'stop looking at the blog')
        steering.set_autonomy('t1', 'full')
        self.assertEqual(steering.take('t1'), 'stop looking at the blog')
        # The steer is gone; the mode is not.
        self.assertEqual(steering.take('t1'), '')
        self.assertEqual(steering.autonomy('t1'), 'full')

    def test_plan_cannot_be_switched_to_mid_run(self):
        # Which tools exist is settled when the toolbox is built, so a mid-run
        # 'plan' could only gate the mutating tools rather than withdraw them
        # — which is `review` under a name that promises more.
        self.assertFalse(steering.set_autonomy('t1', 'plan'))
        self.assertEqual(steering.autonomy('t1'), '')

    def test_unknown_levels_are_refused(self):
        self.assertFalse(steering.set_autonomy('t1', 'yolo'))
        self.assertEqual(steering.autonomy('t1'), '')

    def test_switchable_modes_resolves_every_switchable_level(self):
        modes = switchable_modes(toolbox(webSearch=True, fileOps=True))
        self.assertEqual(set(modes), set(steering.SWITCHABLE))
        for level, (names, policy) in modes.items():
            self.assertIsInstance(names, frozenset)
            self.assertTrue(callable(policy), level)

    def test_no_override_reads_as_empty(self):
        # The common case, hit on every batch of every run that nobody is
        # steering — it has to be cheap and it has to mean "leave it alone".
        self.assertEqual(steering.autonomy('never-seen'), '')


class ApprovalScopeTests(TestCase):
    """once / session / always, and the rung that was missing."""

    def setUp(self):
        self.user = User.objects.create_user('scope', 'scope@example.com', 'pw')

    def _approve(self, scope, **kw):
        from chat.models import ToolPermission
        from chat.turn.agent import approve_tool_call

        # No checkpoint for this thread, so the standing-allowance branch is
        # never reached — which is itself the behaviour under test in
        # `test_a_missing_run_stores_nothing`. The rows below are written by
        # the tests that build a real pause.
        async_to_sync(approve_tool_call)(
            'thread-1', 'call-1', scope=scope, user_id=self.user.id, **kw,
        )
        return ToolPermission.objects.filter(user=self.user)

    def test_a_missing_run_stores_nothing(self):
        # Approving into a thread with no state must not file an allowance for
        # a call nobody can show the user.
        self.assertFalse(self._approve('always').exists())

    def test_serializer_maps_legacy_remember_to_always(self):
        # An older client sends `remember: true` and no scope. If the scope
        # field simply defaulted to 'once', that client's standing allowance
        # would be silently downgraded to a single call.
        from agents.views.runs import AgentApproveSerializer

        s = AgentApproveSerializer(data={
            'thread_id': 't', 'call_id': 'c', 'remember': True,
        })
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['scope'], 'always')

    def test_serializer_defaults_to_once_without_remember(self):
        from agents.views.runs import AgentApproveSerializer

        s = AgentApproveSerializer(data={'thread_id': 't', 'call_id': 'c'})
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['scope'], 'once')

    def test_an_explicit_scope_beats_remember(self):
        from agents.views.runs import AgentApproveSerializer

        s = AgentApproveSerializer(data={
            'thread_id': 't', 'call_id': 'c', 'remember': True, 'scope': 'session',
        })
        self.assertTrue(s.is_valid(), s.errors)
        self.assertEqual(s.validated_data['scope'], 'session')

    def test_session_scoped_allowance_matches_only_its_own_session(self):
        from chat.models import ToolPermission
        from chat.tools.permissions import is_remembered

        ToolPermission.objects.create(
            user=self.user, tool_name='mcp__7__send_email', session_key='sess-a',
        )
        ctx_a = {'user_id': self.user.id, 'session_id': 'sess-a'}
        ctx_b = {'user_id': self.user.id, 'session_id': 'sess-b'}
        self.assertTrue(async_to_sync(is_remembered)('mcp__7__send_email', ctx_a))
        self.assertFalse(async_to_sync(is_remembered)('mcp__7__send_email', ctx_b))

    def test_always_scoped_allowance_matches_every_session(self):
        from chat.models import ToolPermission
        from chat.tools.permissions import is_remembered

        ToolPermission.objects.create(
            user=self.user, tool_name='mcp__7__send_email', session_key='',
        )
        for key in ('sess-a', 'sess-b', ''):
            self.assertTrue(async_to_sync(is_remembered)(
                'mcp__7__send_email', {'user_id': self.user.id, 'session_id': key},
            ), key)


class AutonomyEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('runner', 'runner@example.com', 'pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Researcher', tool_grants={'webSearch': True},
        )
        steering.clear()

    def tearDown(self):
        steering.clear()

    def url(self, agent_id=None):
        return reverse('orchestrator:agent_autonomy',
                       args=[agent_id or self.agent.id])

    def test_no_running_run_is_a_404(self):
        r = self.client.post(self.url(), {'level': 'auto'}, format='json')
        self.assertEqual(r.status_code, 404)

    def test_plan_is_refused_by_the_serializer(self):
        r = self.client.post(self.url(), {'level': 'plan'}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_another_users_agent_is_a_404(self):
        # A thread id is a guess-resistant string, not an authorisation — the
        # same rule `agent_approve` follows.
        other = User.objects.create_user('other', 'other@example.com', 'pw')
        theirs = SubAgent.objects.create(user=other, name='Theirs')
        r = self.client.post(self.url(theirs.id), {'level': 'auto'}, format='json')
        self.assertEqual(r.status_code, 404)

    def test_a_running_run_accepts_the_change(self):
        from logs.models import ExecutionLog

        log = ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running',
            input_data={'thread_id': 'thread-xyz'},
        )
        r = self.client.post(self.url(), {'level': 'full'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['autonomy'], 'full')
        self.assertEqual(r.data['execution_id'], str(log.execution_id))
        self.assertEqual(steering.autonomy('thread-xyz'), 'full')

    def test_a_run_with_no_thread_id_cannot_be_switched(self):
        ExecutionLog = __import__('logs.models', fromlist=['ExecutionLog']).ExecutionLog
        ExecutionLog.objects.create(
            user=self.user, subagent=self.agent, status='running', input_data={},
        )
        r = self.client.post(self.url(), {'level': 'auto'}, format='json')
        self.assertEqual(r.status_code, 409)
