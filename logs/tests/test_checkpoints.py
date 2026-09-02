"""
A finished run must not keep its checkpoints.

`MemorySaver` has no eviction — no maxsize, no TTL — so every super-step of
every run stayed resident for the life of the process. Agent runs made it
sharpest: each gets a fresh uuid thread id and every fanout worker gets
another, so a finished run's checkpoints could never be reached again and
nothing deleted them. The process grew without bound while nothing was leaking
in the ordinary sense, which is why this needs a test rather than a comment.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase
from langchain_core.messages import HumanMessage

from chat.turn.agent import chat_agent_graph, forget_thread


def _checkpoint_count(thread_id: str) -> int:
    saver = chat_agent_graph.checkpointer
    cfg = {"configurable": {"thread_id": thread_id}}
    return len(list(saver.list(cfg)))


def _seed(thread_id: str) -> None:
    """Put one checkpoint on a thread without running a model."""
    saver = chat_agent_graph.checkpointer
    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    saver.put(
        cfg,
        {"v": 1, "id": "cp-1", "ts": "2026-01-01T00:00:00+00:00",
         "channel_values": {"messages": [HumanMessage(content="hi")]},
         "channel_versions": {}, "versions_seen": {}},
        {"source": "input", "step": 0, "parents": {}},
        {},
    )


class ForgetThreadTests(SimpleTestCase):
    def test_a_seeded_thread_is_dropped(self):
        _seed("thread-to-drop")
        self.assertGreater(_checkpoint_count("thread-to-drop"), 0)

        self.assertTrue(async_to_sync(forget_thread)("thread-to-drop"))
        self.assertEqual(_checkpoint_count("thread-to-drop"), 0)

    def test_dropping_one_thread_leaves_its_neighbours_alone(self):
        # Workers share nothing but the process; dropping one must not take out
        # a sibling still running.
        _seed("keep-me")
        _seed("drop-me")

        async_to_sync(forget_thread)("drop-me")

        self.assertEqual(_checkpoint_count("drop-me"), 0)
        self.assertGreater(_checkpoint_count("keep-me"), 0)

    def test_an_unknown_thread_is_not_an_error(self):
        # Called on every run close, including runs that never checkpointed.
        self.assertTrue(async_to_sync(forget_thread)("never-existed"))

    def test_failure_is_swallowed(self):
        # Freeing memory must never fail a run that already has its answer.
        saver = chat_agent_graph.checkpointer
        original = type(saver).adelete_thread

        async def boom(self, thread_id):
            raise RuntimeError("checkpointer is unhappy")

        type(saver).adelete_thread = boom
        try:
            self.assertFalse(async_to_sync(forget_thread)("anything"))
        finally:
            type(saver).adelete_thread = original


class RunEndsDropCheckpointsTests(SimpleTestCase):
    """The wiring, not the helper: every terminal path has to call it."""

    def test_all_three_run_end_paths_forget_the_thread(self):
        import inspect

        from agents.agent import runtime

        source = inspect.getsource(runtime)
        # Completed, failed and cancelled all end the thread for good. A
        # *paused* run deliberately keeps its checkpoint — that is what the
        # approval resumes from — so the guard matters as much as the calls.
        self.assertGreaterEqual(source.count("await forget_thread("), 3)
        self.assertIn("if status != 'paused':", source)

        for path in ("status='failed'", "status='cancelled'"):
            self.assertIn(path, source)
