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
import json
import time
from dataclasses import replace
from unittest.mock import patch

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


async def _no_images(query, *_args, **_kwargs) -> list:
    """The companion strip, silenced: these tests are about dispatch timing,
    not the image catalogue, and a live search would put real network time
    inside every timing assertion. Tests that care about the strip patch it
    themselves (an inner patch wins over this one)."""
    return []


def run_tools(calls, turn) -> dict:
    """Drive `tools_node` with one assistant turn's worth of calls."""
    state = {
        "messages": [AIMessage(content="", tool_calls=list(calls))],
        "metadata": {}, "tool_trace": [], "thinking": "", "total_tokens": 0,
    }
    with patch("chat.sources.search.image_search", _no_images):
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
        # A name that parses as nothing at all: not a built-in, and not the
        # `mcp__<server id>__<tool>` shape either, so there is no claim of any
        # kind to act on and it fails closed.
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


class ReadOnlyMCPCallsOverlapTests(SimpleTestCase):
    """The connector half of parallel dispatch.

    `PARALLEL_TOOLS` is a declaration, and an MCP tool can never make one — its
    name is minted at runtime by a third party. That left every connector call
    serial, so three reads against one server cost three full round trips end
    to end, which is the single largest avoidable delay in a connector-heavy
    turn.

    The claim these tests pin is narrow: a call whose *name* says it observes
    may overlap; everything else still goes one at a time. It is the same claim
    `permissions.default_policy` already trusts to decide whether a credentialed
    call is gated at all — applied to a weaker question, where a wrong guess
    reorders work that was going to happen anyway rather than skipping a human.
    """

    #: The real encoding, from `mcp_integration.tool_provider.encode_tool_name`:
    #: `mcp__<server id>__<sanitised name>_<8-char digest>`. Written out rather
    #: than computed so the test still fails if the *shape* changes — the
    #: predicate reads the name and nothing else, so the shape is the contract.
    LIST = "mcp__7__list_messages_b03c142c"
    SEARCH = "mcp__7__search_files_17e706ef"
    SEND = "mcp__7__send_message_16c3edb3"
    DELETE = "mcp__7__delete_file_10e58340"

    def test_two_reads_against_one_server_overlap(self):
        rec = DispatchRecorder()

        started = time.monotonic()
        run_tools([call(self.LIST, "a"), call(self.SEARCH, "b")], make_turn(rec))
        elapsed = time.monotonic() - started

        self.assertEqual(rec.peak_in_flight, 2)
        self.assertLess(elapsed, DELAY * 2)

    def test_writes_stay_serial(self):
        rec = DispatchRecorder()
        run_tools([call(self.SEND, "a"), call(self.DELETE, "b")], make_turn(rec))
        self.assertEqual(rec.peak_in_flight, 1)

    def test_a_read_does_not_overlap_a_write(self):
        """Only the reads join; the write keeps its own slot."""
        rec = DispatchRecorder()
        run_tools(
            [call(self.LIST, "a"), call(self.SEND, "b"), call(self.SEARCH, "c")],
            make_turn(rec),
        )
        self.assertEqual(rec.peak_in_flight, 2)

    def test_a_sensitive_read_never_overlaps(self):
        """An agent on `review` marks everything sensitive. The name saying
        `list` does not outrank a human having been asked about it."""
        rec = DispatchRecorder()
        turn = make_turn(rec, sensitive=frozenset({self.LIST, self.SEARCH}))
        state = {
            "messages": [AIMessage(content="", tool_calls=[
                call(self.LIST, "a"), call(self.SEARCH, "b")])],
            "metadata": {"approved_tool_calls": ["a", "b"]},
            "tool_trace": [], "thinking": "", "total_tokens": 0,
        }
        async_to_sync(tools_node)(state, {"configurable": {"turn": turn}})
        self.assertEqual(rec.peak_in_flight, 1)

    def test_results_are_recorded_in_call_order_not_completion_order(self):
        """The reason recording is a separate pass. `_apply_side_effects` does a
        read-modify-write on one shared `meta`, so completion order would
        reshuffle the run's own record between runs of the same turn."""
        rec = DispatchRecorder()
        out = run_tools(
            [call(self.SEARCH, "first"), call(self.LIST, "second")], make_turn(rec),
        )
        names = [m.tool_call_id for m in out["messages"]]
        self.assertEqual(names, ["first", "second"])


class ReadOnlyNamePredicateTests(SimpleTestCase):
    """`permissions.mcp_reads_only` — the guess, and where it stops."""

    def test_it_reads_the_original_name_through_the_digest(self):
        from chat.tools import permissions

        self.assertTrue(permissions.mcp_reads_only("mcp__7__list_messages_b03c142c"))
        self.assertFalse(permissions.mcp_reads_only("mcp__7__send_message_16c3edb3"))

    def test_a_built_in_is_never_judged_by_its_name(self):
        """Built-ins declare `parallel` on `@tool()`; a declaration by the
        person who wrote the tool beats a guess about its name, so this
        predicate declines to have an opinion about them."""
        from chat.tools import permissions

        self.assertFalse(permissions.mcp_reads_only("read_url"))
        self.assertFalse(permissions.mcp_reads_only("list_knowledge_bases"))

    def test_an_unparseable_name_fails_closed(self):
        from chat.tools import permissions

        self.assertFalse(permissions.mcp_reads_only("mcp__slack__post"))
        self.assertFalse(permissions.mcp_reads_only(""))

    def test_a_destructive_name_wearing_a_read_prefix_is_a_known_hole(self):
        """`list_and_purge` passes, exactly as it does for approval gating.

        Pinned rather than fixed: nothing about a name can prove what a
        third-party endpoint does. What makes it tolerable *here* and not there
        is the consequence — this call was already going to run in this turn;
        only its overlap with a sibling changed.
        """
        from chat.tools import permissions

        self.assertTrue(permissions.mcp_reads_only("mcp__7__list_and_purge_4a4eb864"))


class CompanionImageOverlapTests(SimpleTestCase):
    """The image strip for a web search starts with the search, not after it.

    `_on_web_search` awaited `image_search(query)` after every web search —
    +1–2s serially after the tool the model actually needed, before the next
    model iteration. The strip is an independent network round trip whose
    query is known at plan time, so `tools_node` starts it in Pass 3
    alongside the dispatches and Pass 4 only collects it — into the same
    `meta` write that persists, never detached.
    """

    SEARCH_RESULT = json.dumps({
        "type": "search_results", "text": "t",
        "sources": [{"url": "http://e", "title": "E"}],
    })
    ONE_IMAGE = [{"title": "eagles", "image": "http://img",
                  "url": "http://p", "source": "s"}]

    class _SearchAfterImageStart:
        """A `web_search` dispatch gated on the companion being in flight.

        Under the old serial order the companion started only after every
        dispatch finished, so this burns its whole timeout; overlapped, it
        proceeds as soon as the strip starts.
        """

        def __init__(self, started: asyncio.Event, timeout: float = 5):
            self.started = started
            self.timeout = timeout
            self.timed_out = False

        async def __call__(self, name: str, args: dict, context: dict) -> str:
            try:
                await asyncio.wait_for(self.started.wait(), self.timeout)
            except asyncio.TimeoutError:
                self.timed_out = True
            return CompanionImageOverlapTests.SEARCH_RESULT

    def _run_search(self, dispatcher, images) -> dict:
        """Drive one `web_search` turn with `images` as the image provider.

        `run_tools` silences the strip for timing hermeticity, so these tests
        drive `tools_node` directly: an outer silence patch would win over the
        mock under test (last patch entered wins).
        """
        state = {
            "messages": [AIMessage(content="", tool_calls=[
                call("web_search", "s", query="eagles")])],
            "metadata": {}, "tool_trace": [], "thinking": "", "total_tokens": 0,
        }
        turn = make_turn(dispatcher)
        with patch("chat.sources.search.image_search", images):
            return async_to_sync(tools_node)(
                state, {"configurable": {"turn": turn}})

    def test_the_strip_runs_alongside_the_search_not_after_it(self):
        started = asyncio.Event()
        dispatcher = self._SearchAfterImageStart(started)

        async def _images(query, *_args, **_kwargs):
            started.set()
            await asyncio.sleep(0.2)
            return list(self.ONE_IMAGE)

        begun = time.monotonic()
        out = self._run_search(dispatcher, _images)
        elapsed = time.monotonic() - begun

        self.assertFalse(dispatcher.timed_out,
                         "the companion started only after the dispatch finished")
        # Serial would have burned the whole 5s gate above; overlapped, the
        # turn costs the strip's own 0.2s and nothing on top of the search.
        self.assertLess(elapsed, 4)
        self.assertEqual(out["metadata"]["images"], self.ONE_IMAGE)
        self.assertEqual(out["metadata"]["search_query"], "eagles")

    def test_no_companion_when_the_model_asked_for_images_itself(self):
        """An explicit `image_search` already fills the strip; a second query
        for it would be pure spend."""
        with patch("chat.sources.search.image_search") as images:
            run_tools([call("web_search", "s", query="eagles"),
                       call("image_search", "i", query="eagles")],
                      make_turn(DispatchRecorder()))
        images.assert_not_called()

    def test_the_side_effect_still_searches_inline_with_no_prefetch(self):
        """Direct callers of `_apply_side_effects` pass no companion: the
        strip must still fill inline rather than stay empty."""
        from chat.turn.agent import _apply_side_effects, null_sink

        async def _images(query, *_args, **_kwargs):
            return list(self.ONE_IMAGE)

        meta: dict = {}
        with patch("chat.sources.search.image_search", _images):
            async_to_sync(_apply_side_effects)(
                "web_search", {"query": "eagles"}, self.SEARCH_RESULT,
                meta, null_sink,
            )
        self.assertEqual(meta["images"], self.ONE_IMAGE)

    def test_a_failed_prefetch_leaves_the_search_result_intact(self):
        """A dead image provider costs the strip, never the search."""

        async def _search(name, args, context):
            return self.SEARCH_RESULT

        async def _boom(query, *_args, **_kwargs):
            raise RuntimeError("images are down")

        out = self._run_search(_search, _boom)
        # No images key at all: `_collect_media` with `[]` appends nothing,
        # and the sources the model actually needed are untouched.
        self.assertNotIn("images", out["metadata"])
        self.assertEqual(len(out["metadata"]["sources"]), 1)
