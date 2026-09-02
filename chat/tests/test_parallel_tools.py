"""
Parallel tool dispatch.

A model issues every call in a turn *before* it has seen any result, so no call
in a batch can depend on another one — overlapping them is safe by
construction. The runtime nevertheless ran them one at a time, which made three
web searches in one turn cost three round trips instead of one.

These tests pin both halves: that the safe ones really do overlap, and that
nothing the model or the user sees depends on which finished first.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from langchain_core.messages import AIMessage

from chat.turn.agent import TurnContext, tools_node

#: Long enough that N sequential calls are unmistakably slower than N parallel
#: ones, short enough that the suite does not crawl.
DELAY = 0.15


class DispatchRecorder:
    """A tool dispatcher that sleeps, and records when each call ran."""

    def __init__(self, delay: float = DELAY):
        self.delay = delay
        self.started: list[str] = []
        self.finished: list[str] = []
        self.contexts: dict[str, dict] = {}
        self.peak_in_flight = 0
        self._in_flight = 0

    async def __call__(self, name: str, args: dict, context: dict) -> str:
        key = context.get("call_id") or name
        # Captured, not referenced: the point is to prove each call saw its own
        # `call_id`, which a shared mutable context would not give it.
        self.contexts[key] = dict(context)
        self.started.append(key)
        self._in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self._in_flight -= 1
        self.finished.append(key)
        return f"result of {name} for {key}"


def make_turn(dispatcher, *, sensitive=frozenset()) -> TurnContext:
    return TurnContext(
        provider="test", model="test-model", system_message="sys",
        user_id=1, session_id="thread-1", intent="research", user_text="q",
        tool_dispatch=dispatcher, sensitive_tools=sensitive,
        # Nothing may pause: `permissions.never` is the policy an
        # `autonomy='full'` agent runs under, and an approval interrupt inside
        # a unit test would just hang.
        approval_policy=_never,
    )


async def _never(name, args, context) -> bool:
    return False


def run_tools(calls, turn) -> dict:
    """Drive `tools_node` with one assistant turn's worth of calls."""
    state = {
        "messages": [AIMessage(content="", tool_calls=list(calls))],
        "metadata": {}, "tool_trace": [], "thinking": "", "total_tokens": 0,
    }
    return async_to_sync(tools_node)(state, {"configurable": {"turn": turn}})


def call(name: str, cid: str, **args) -> dict:
    return {"name": name, "id": cid, "args": args or {"query": cid}}


class ParallelDispatchTests(SimpleTestCase):
    def test_read_only_calls_overlap(self):
        rec = DispatchRecorder()
        calls = [call("web_search", "a"), call("web_search", "b"),
                 call("read_url", "c")]

        started = time.monotonic()
        run_tools(calls, make_turn(rec))
        elapsed = time.monotonic() - started

        self.assertEqual(rec.peak_in_flight, 3)
        # Three sequential sleeps would be 3 * DELAY; parallel is ~1.
        self.assertLess(elapsed, DELAY * 2)

    def test_unsafe_calls_do_not_overlap(self):
        # `execute_python` captures output by swapping the process-global
        # `sys.stdout`, so two concurrent runs interleave each other's stdout.
        rec = DispatchRecorder()
        calls = [call("execute_python", "a", code="1"),
                 call("execute_python", "b", code="2")]

        started = time.monotonic()
        run_tools(calls, make_turn(rec))
        elapsed = time.monotonic() - started

        self.assertEqual(rec.peak_in_flight, 1)
        # Wall-clock corroboration, with a tolerance. `asyncio.sleep` is allowed
        # to return a hair early — on Windows the loop's timer granularity is
        # ~15.6ms — so two serial 150ms sleeps land just under 300ms about once
        # in forty runs, which made this the suite's one intermittent failure.
        # The margin still catches what it is for: overlapping calls finish in
        # ~150ms, less than half the floor below.
        self.assertGreaterEqual(elapsed, DELAY * 2 * 0.9)

    def test_unknown_names_stay_serial(self):
        # Every MCP tool lands here: its name is minted at runtime from a
        # third-party catalogue, so it can never carry the `parallel` flag.
        rec = DispatchRecorder()
        run_tools([call("mcp__slack__post", "a"), call("mcp__slack__post", "b")],
                  make_turn(rec))
        self.assertEqual(rec.peak_in_flight, 1)

    def test_sensitive_calls_never_overlap_even_if_declared_parallel(self):
        # `read_file` is declared parallel-safe, but an agent on autonomy
        # 'review' marks everything sensitive — and a call worth pausing a
        # human for is one with an effect that may well be ordered.
        rec = DispatchRecorder()
        turn = make_turn(rec, sensitive=frozenset({"read_file"}))
        # Pre-approved, so the gate does not interrupt.
        state = {
            "messages": [AIMessage(content="", tool_calls=[
                call("read_file", "a", path="x"), call("read_file", "b", path="y")])],
            "metadata": {"approved_tool_calls": ["a", "b"]},
            "tool_trace": [], "thinking": "", "total_tokens": 0,
        }
        async_to_sync(tools_node)(state, {"configurable": {"turn": turn}})
        self.assertEqual(rec.peak_in_flight, 1)

    def test_a_mixed_batch_runs_the_safe_half_together(self):
        rec = DispatchRecorder()
        calls = [call("web_search", "a"), call("execute_python", "b", code="1"),
                 call("web_search", "c")]
        run_tools(calls, make_turn(rec))
        # The two searches overlap; the sandbox call does not join them.
        self.assertEqual(rec.peak_in_flight, 2)

    def test_a_single_call_is_not_gathered(self):
        rec = DispatchRecorder()
        run_tools([call("web_search", "only")], make_turn(rec))
        self.assertEqual(rec.peak_in_flight, 1)


class OrderingTests(SimpleTestCase):
    """Parallel dispatch, deterministic everything-else."""

    def test_results_keep_call_order_not_completion_order(self):
        # The transcript must match the assistant turn's call ids in order; a
        # reshuffle would also make the model's "the third result" meaningless.
        class Reversed(DispatchRecorder):
            async def __call__(self, name, args, context):
                # Later calls finish first.
                delay = {"a": 0.15, "b": 0.10, "c": 0.01}[context["call_id"]]
                await asyncio.sleep(delay)
                self.finished.append(context["call_id"])
                return f"answer-{context['call_id']}"

        rec = Reversed()
        out = run_tools([call("web_search", "a"), call("web_search", "b"),
                         call("web_search", "c")], make_turn(rec))

        self.assertEqual(rec.finished, ["c", "b", "a"])          # completion
        self.assertEqual([m.tool_call_id for m in out["messages"]],
                         ["a", "b", "c"])                        # transcript
        self.assertEqual([e["call_id"] for e in out["tool_trace"]],
                         ["a", "b", "c"])                        # what the UI saw

    def test_each_call_gets_its_own_call_id(self):
        # The bug this guards: `call_id` was written onto one shared context
        # dict just before each dispatch. Concurrently, siblings overwrite it —
        # and `invoke_subagent` reads it to record which call spawned a worker,
        # so a race there misattributes entire runs.
        rec = DispatchRecorder()
        run_tools([call("web_search", "a"), call("web_search", "b"),
                   call("web_search", "c")], make_turn(rec))

        self.assertEqual(set(rec.contexts), {"a", "b", "c"})
        for cid, ctx in rec.contexts.items():
            self.assertEqual(ctx["call_id"], cid)

    def test_observers_run_in_call_order(self):
        # One `AgentStep` row is written per call by this observer. In
        # completion order the run's own record would reshuffle between runs of
        # the same turn.
        seen: list[str] = []

        async def observer(**kw):
            seen.append(kw["call_id"])

        class Reversed(DispatchRecorder):
            async def __call__(self, name, args, context):
                await asyncio.sleep({"a": 0.12, "b": 0.01}[context["call_id"]])
                return "x"

        turn = replace(make_turn(Reversed()), on_tool_result=observer)
        run_tools([call("web_search", "a"), call("web_search", "b")], turn)

        self.assertEqual(seen, ["a", "b"])

    def test_a_failing_call_does_not_take_down_its_siblings(self):
        class OneBad(DispatchRecorder):
            async def __call__(self, name, args, context):
                if context["call_id"] == "b":
                    raise RuntimeError("boom")
                return f"ok-{context['call_id']}"

        out = run_tools([call("web_search", "a"), call("web_search", "b"),
                         call("web_search", "c")], make_turn(OneBad()))

        bodies = {m.tool_call_id: m.content for m in out["messages"]}
        self.assertEqual(len(bodies), 3)
        self.assertIn("ok-a", bodies["a"])
        self.assertIn("boom", bodies["b"])
        self.assertIn("ok-c", bodies["c"])


class RegistryDeclarationTests(SimpleTestCase):
    def test_parallel_is_an_allow_list_not_a_deny_list(self):
        from chat.tools import PARALLEL_TOOLS
        from chat.tools.registry import all_tools

        names = {t.name for t in all_tools()}
        self.assertTrue(PARALLEL_TOOLS <= names)
        # An unknown name is serial, which is what keeps MCP safe.
        self.assertNotIn("mcp__anything__do", PARALLEL_TOOLS)

    def test_no_sensitive_tool_is_declared_parallel(self):
        # Belt and braces with the runtime check: the two must not disagree.
        from chat.tools import PARALLEL_TOOLS, SENSITIVE_TOOLS

        self.assertEqual(PARALLEL_TOOLS & frozenset(SENSITIVE_TOOLS), frozenset())

    def test_the_sandbox_is_not_parallel(self):
        # `redirect_stdout` swaps a process global; two concurrent executions
        # would interleave each other's captured output.
        from chat.tools import PARALLEL_TOOLS

        self.assertNotIn("execute_python", PARALLEL_TOOLS)
