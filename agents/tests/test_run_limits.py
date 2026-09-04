"""
What one run may take of the box: wall-clock time, and a concurrency slot.

These are the two limits the builder's "Resources" panel used to promise and
never delivered. CPUs and Memory (MB) were stored, validated, round-tripped to
the UI and read by nothing — the panel carried an orange "COMING SOON" badge
saying so — and neither could ever have been enforced, because agent code runs
on a thread inside the Django process where there is no cgroup to hang a quota
off. Meanwhile the thing a run genuinely holds for minutes at a time had no
limit at all.

So the cases here are about the two properties that actually matter:

- a run cannot outlive its limit, and stops in a way that still returns work;
- a *sub*-agent cannot outlive the run that asked for it, however it is
  configured — which is why time is shared from a parent's clock rather than
  read fresh from each worker's own row.
"""
import asyncio
import time

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from agents import admission, budget
from agents.models import SubAgent
from workflow_backend.thresholds import (
    DEFAULT_RUN_SECONDS,
    MAX_CONCURRENT_RUNS_PER_USER,
    MAX_RUN_SECONDS,
    MIN_RUN_SECONDS,
    MIN_WORKER_SECONDS,
    RUN_WRAPUP_SECONDS,
)


class ConfiguredLimitTests(SimpleTestCase):
    """Reading the knob, including from rows saved before it existed."""

    def test_an_agent_with_no_setting_gets_the_default(self):
        # Every agent saved before `maxRunSeconds` existed has no key at all,
        # and all of them still have to run. A KeyError on this path would take
        # out the entire existing install on its next run.
        self.assertEqual(budget.limit_for(SubAgent(guardrails={})), DEFAULT_RUN_SECONDS)
        self.assertEqual(budget.limit_for(SubAgent(guardrails=None)), DEFAULT_RUN_SECONDS)

    def test_a_configured_limit_is_honoured(self):
        self.assertEqual(budget.limit_for(SubAgent(guardrails={'maxRunSeconds': 300})), 300)

    def test_values_outside_the_range_are_clamped_not_rejected(self):
        # This is read at run time; the serializer already refused anything out
        # of range at save time. Whatever reaches here got in some other way (a
        # fixture, a shell, an older client), and clamping runs the agent while
        # refusing would strand it.
        self.assertEqual(budget.clamp_run_seconds(1), MIN_RUN_SECONDS)
        self.assertEqual(budget.clamp_run_seconds(10 ** 9), MAX_RUN_SECONDS)
        self.assertEqual(budget.clamp_run_seconds('nonsense'), DEFAULT_RUN_SECONDS)
        self.assertEqual(budget.clamp_run_seconds(None), DEFAULT_RUN_SECONDS)


class DeadlineTests(SimpleTestCase):
    def test_it_counts_down(self):
        d = budget.Deadline.after(600)
        self.assertAlmostEqual(d.remaining(), 600, delta=2)
        self.assertFalse(d.expired)
        self.assertFalse(d.wrapping_up)

    def test_wrapping_up_fires_before_expiry_not_at_it(self):
        # The gap is the whole design. A run stopped at zero is killed
        # mid-tool-call, having paid for every call it made and returning none
        # of what they found; one that stops asking for tools while there is
        # still room to write gets to hand back what it has.
        nearly = budget.Deadline.after(RUN_WRAPUP_SECONDS - 5)
        self.assertTrue(nearly.wrapping_up)
        self.assertFalse(nearly.expired)

    def test_remaining_never_goes_negative(self):
        past = budget.Deadline(at=time.monotonic() - 100, limit=60)
        self.assertEqual(past.remaining(), 0.0)
        self.assertTrue(past.expired)

    def test_it_cannot_be_extended_from_inside(self):
        # Frozen on purpose: this object is handed to the graph it bounds, and
        # a run that can push back its own deadline has no deadline.
        with self.assertRaises(Exception):
            budget.Deadline.after(60).at = time.monotonic() + 10_000


class WorkerDeadlineTests(SimpleTestCase):
    """A subagent cannot outlast the run that called it."""

    def test_a_worker_is_capped_by_the_parent_not_by_its_own_setting(self):
        # The case the whole feature exists for: an agent configured for an
        # hour, invoked by a run with four minutes left. It gets the four
        # minutes, never the hour.
        parent = budget.Deadline.after(240)
        child = parent.child(3600)
        self.assertLess(child.remaining(), 240)
        self.assertGreater(child.remaining(), 0)

    def test_a_shorter_worker_setting_still_wins(self):
        # `min`, not "the parent's share": a worker deliberately configured for
        # 60s must not be widened by being called from a long-running parent.
        child = budget.Deadline.after(3600).child(60)
        self.assertAlmostEqual(child.remaining(), 60, delta=2)

    def test_the_parent_keeps_time_to_read_the_answers(self):
        # A fan-out allowed to run until the parent's clock hits zero returns
        # eight answers into a run with no turn left to synthesise them — full
        # cost, no result, which is worse than not delegating at all.
        parent = budget.Deadline.after(600)
        child = parent.child(None)
        self.assertLess(child.remaining(), parent.remaining())
        reserve = parent.remaining() - child.remaining()
        self.assertGreater(reserve, RUN_WRAPUP_SECONDS)

    def test_delegating_with_no_time_left_is_refused_not_attempted(self):
        # N workers that each die on their first model call is a worse answer
        # for the model to read than one sentence telling it to wrap up.
        with self.assertRaises(budget.OutOfTime):
            budget.Deadline.after(MIN_WORKER_SECONDS).child(600)

    def test_a_chain_cannot_escape_by_going_deeper(self):
        # Depth is bounded separately, but the time bound has to hold on its
        # own: each hop takes a share of what is *left*, so the sequence is
        # strictly decreasing however many hops there are.
        parent = budget.Deadline.after(1200)
        child = parent.child(9999)
        grandchild = child.child(9999)
        self.assertLess(grandchild.remaining(), child.remaining())
        self.assertLess(child.remaining(), parent.remaining())


class SoftStopTests(SimpleTestCase):
    """Out of time takes the same last pass as out of iterations."""

    def _turn(self, deadline):
        from chat.turn.agent import TurnContext

        return TurnContext(
            provider='openrouter', model='m', system_message='', user_id=1,
            session_id='s', intent='research', user_text='go', deadline=deadline,
        )

    def test_a_live_deadline_leaves_the_loop_alone(self):
        self.assertFalse(self._turn(budget.Deadline.after(600)).deadline.wrapping_up)

    def test_a_spent_deadline_puts_the_turn_on_its_last_pass(self):
        self.assertTrue(self._turn(budget.Deadline.after(1)).deadline.wrapping_up)

    def test_chat_has_no_deadline_and_is_unaffected(self):
        from chat.turn.agent import TurnContext

        turn = TurnContext(provider='p', model='m', system_message='', user_id=1,
                           session_id='s', intent='chat', user_text='hi')
        self.assertIsNone(turn.deadline)

    def test_the_wording_names_time_not_the_tool_limit(self):
        # The two ways to reach the last pass are separate facts, and the model
        # relays whichever it was told. Telling someone whose agent ran out of
        # clock that it hit a "tool-call limit" sends them to raise the one
        # knob that was never the constraint.
        from chat.turn import prompts

        self.assertIn('time limit', prompts.CONTINUE_OUT_OF_TIME)
        self.assertNotIn('time limit', prompts.CONTINUE_AT_LIMIT)


class TimeoutReportingTests(SimpleTestCase):
    def test_a_timed_out_run_has_a_status_of_its_own(self):
        # `cancelled` means a person pressed stop; `timeout` means the system
        # stopped it and the owner can raise the limit. Reporting either as the
        # other sends whoever reads the run to the wrong place.
        from logs.models import ExecutionLog

        statuses = dict(ExecutionLog.STATUS_CHOICES)
        self.assertIn('timeout', statuses)
        self.assertIn('cancelled', statuses)

    def test_the_message_says_which_limit_and_where_to_change_it(self):
        message = budget.describe(budget.Deadline.after(600))
        self.assertIn('10 minutes', message)
        self.assertIn('Time limit', message)


class AdmissionTests(SimpleTestCase):
    """One account cannot be every run on the box."""

    def setUp(self):
        admission._reset_for_tests()

    def tearDown(self):
        admission._reset_for_tests()

    @staticmethod
    async def _settle():
        """Let every pending task reach its next await."""
        for _ in range(4):
            await asyncio.sleep(0)

    def test_runs_beyond_the_per_user_limit_wait_for_a_slot(self):
        async def scenario():
            started = []
            gate = asyncio.Event()

            async def occupy(tag):
                async with admission.slot(1):
                    started.append(tag)
                    await gate.wait()

            held = [asyncio.ensure_future(occupy(f'first-{i}'))
                    for i in range(MAX_CONCURRENT_RUNS_PER_USER)]
            await self._settle()

            queued = asyncio.ensure_future(occupy('queued'))
            await self._settle()
            self.assertNotIn('queued', started)

            gate.set()
            await asyncio.gather(*held, queued)
            self.assertIn('queued', started)

        async_to_sync(scenario)()

    def test_another_user_is_not_blocked_by_the_first(self):
        # The fairness property. If one account filling its own quota stalled
        # everyone else, the gate would be making things worse, not better.
        async def scenario():
            gate = asyncio.Event()
            ran = []

            async def occupy(user_id):
                async with admission.slot(user_id):
                    ran.append(user_id)
                    await gate.wait()

            busy = [asyncio.ensure_future(occupy(1))
                    for _ in range(MAX_CONCURRENT_RUNS_PER_USER)]
            await self._settle()

            other = asyncio.ensure_future(occupy(2))
            await self._settle()
            self.assertIn(2, ran)

            gate.set()
            await asyncio.gather(*busy, other)

        async_to_sync(scenario)()

    def test_waiting_for_a_slot_is_bounded(self):
        # A run that queues for ever is indistinguishable, from the outside,
        # from one that is running — and the caller already has a 202 and an
        # execution id telling it something is happening.
        async def scenario():
            gate = asyncio.Event()

            async def occupy():
                async with admission.slot(1):
                    await gate.wait()

            busy = [asyncio.ensure_future(occupy())
                    for _ in range(MAX_CONCURRENT_RUNS_PER_USER)]
            await self._settle()

            with self.assertRaises(admission.AdmissionTimeout):
                async with admission.slot(1, wait=0.05):
                    pass  # pragma: no cover — admission refuses before the body

            gate.set()
            await asyncio.gather(*busy)

        async_to_sync(scenario)()

    def test_a_refused_admission_leaks_no_slot(self):
        # The failure path releases the global gate it took before waiting on
        # the per-user one. Without that, every refusal would permanently
        # shrink the box's capacity until a restart.
        async def scenario():
            before = admission.snapshot()['global_free']
            gate = asyncio.Event()

            async def occupy():
                async with admission.slot(1):
                    await gate.wait()

            busy = [asyncio.ensure_future(occupy())
                    for _ in range(MAX_CONCURRENT_RUNS_PER_USER)]
            await self._settle()

            for _ in range(3):
                with self.assertRaises(admission.AdmissionTimeout):
                    async with admission.slot(1, wait=0.02):
                        pass  # pragma: no cover

            gate.set()
            await asyncio.gather(*busy)
            self.assertEqual(admission.snapshot()['global_free'], before)

        async_to_sync(scenario)()


class QueuedTelemetryTests(TestCase):
    """A run that waits for a slot says so on its own stream.

    Until `workflow_start` a queued run and a run whose task died between the
    202 and its first frame look identical from outside. The queued frame is
    what separates them — so it is announced before waiting, not after being
    admitted, and a failure to announce it never fails the run.
    """

    def test_a_run_is_announced_queued_before_it_waits_for_a_slot(self):
        import unittest.mock

        import agents.agent.runtime as runtime
        import agents.agent.stream as stream_module
        import workflow_backend.background as background

        user = User.objects.create_user(username='queued', password='pw')
        agent = SubAgent.objects.create(user=user, name='A')

        events: list[str] = []

        class _FakeStream:
            def __init__(self, log):
                pass

            async def run_queued(self):
                events.append('queued')

            async def run_finished(self, **kwargs):
                events.append('finished')

        async def _fake_run_agent(*args, **kwargs):
            events.append('admitted')

        async def _noop(*args, **kwargs):
            return None

        spawned = []
        real = (
            runtime.run_agent, runtime.check_guardrails,
            runtime._check_unattended, background.spawn, stream_module.AgentRunStream,
        )
        runtime.run_agent = _fake_run_agent
        runtime.check_guardrails = _noop
        runtime._check_unattended = _noop
        background.spawn = lambda coro, name='': spawned.append(coro)
        stream_module.AgentRunStream = _FakeStream
        try:
            with unittest.mock.patch('llm.access.preflight', new=_noop):
                execution_id = async_to_sync(runtime.start_agent_run)(
                    agent, 'do it', user=user,
                )
                self.assertTrue(execution_id)

                async def _drive():
                    await spawned[0]

                # `spawn` is stubbed, so drive the detached coroutine here.
                async_to_sync(_drive)()
        finally:
            (
                runtime.run_agent, runtime.check_guardrails,
                runtime._check_unattended, background.spawn,
                stream_module.AgentRunStream,
            ) = real

        self.assertEqual(events, ['queued', 'admitted'])

    def test_a_queued_announcement_cannot_fail_the_run(self):
        # Best-effort like every other frame: a broken channel must not fail
        # a run that is otherwise fine.
        import unittest.mock

        import agents.agent.stream as stream_module

        class _FailingBroadcaster:
            async def workflow_queued(self, execution_id):
                raise RuntimeError('boom')

        log = unittest.mock.Mock(execution_id='exec-1')
        stream = stream_module.AgentRunStream(
            log, broadcaster=_FailingBroadcaster(),
        )
        async_to_sync(stream.run_queued)()


class WorkerAdmissionTests(SimpleTestCase):
    """Workers are deliberately outside the gate — see agents/admission.py."""

    def test_delegated_runs_do_not_pass_through_admission(self):
        # A worker queueing behind the parent that is awaiting it is a
        # deadlock, not a limit. `invoke_subagent` calls `run_agent` directly
        # and must keep doing so; only `start_agent_run` takes a slot.
        import inspect

        from agents.agent import runtime

        self.assertIn('admission.slot', inspect.getsource(runtime.start_agent_run))
        self.assertNotIn('admission', inspect.getsource(runtime.run_agent))


class RunLimitRoundTripTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')

    def test_the_saved_value_is_the_one_the_runtime_reads(self):
        # The mistake `spendCapRupees` made for months: the UI wrote one thing
        # and the guardrail read another, so the knob moved nothing. One read,
        # through `budget.limit_for`, serves both.
        from agents.views.agents import AgentSerializer

        agent = SubAgent.objects.create(
            user=self.user, name='A', guardrails={'maxRunSeconds': 900},
        )
        self.assertEqual(AgentSerializer.to_config(agent)['maxRunSeconds'], 900)
        self.assertEqual(budget.limit_for(agent), 900)

    def test_the_retired_knobs_are_gone_from_the_wire(self):
        # They were saved and never read, behind a badge admitting it. Leaving
        # them accepted-but-ignored would keep the same lie one layer down.
        from agents.views.agents import AgentSerializer

        fields = AgentSerializer().fields
        self.assertNotIn('cpu', fields)
        self.assertNotIn('memoryMb', fields)
        self.assertIn('maxRunSeconds', fields)
