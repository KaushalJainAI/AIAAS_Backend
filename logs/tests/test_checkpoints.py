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
from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
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


class ThreadIdIsAddressableTests(TestCase):
    """The indexed column, and the JSON copy it mirrors.

    `thread_id` lived only inside `input_data`, and three hot paths filtered on
    `input_data__thread_id=` — resuming a paused run, closing a HITL request,
    and resolving a delegated run's parent step. On SQLite that is a full table
    scan with a JSON parse per row, growing with every run the account has ever
    made, paid on the two paths a person is actively waiting on.
    """

    def setUp(self):
        from django.contrib.auth.models import User

        from agents.models import SubAgent

        self.user = User.objects.create_user(username='threaded', password='pw')
        self.agent = SubAgent.objects.create(
            user=self.user, name='Threaded', prompt='Go.',
            llm_provider='nvidia', llm_model='m',
        )

    def _open(self, thread_id: str):
        from agents.agent.runtime import _open_log

        return async_to_sync(_open_log)(
            self.agent, self.user, 'goal', 'manual', thread_id,
        )

    def test_opening_a_run_writes_both_copies(self):
        """The JSON copy stays: it is what historical rows carry and what the
        run's own record shows. The column is the addressable one."""
        log = self._open('agent-9-abcdef')

        self.assertEqual(log.thread_id, 'agent-9-abcdef')
        self.assertEqual(log.input_data['thread_id'], 'agent-9-abcdef')

    def test_a_paused_run_is_found_by_the_column(self):
        from agents.agent.runtime import _find_paused_log
        from logs.models import ExecutionLog

        log = self._open('agent-9-findme')
        ExecutionLog.objects.filter(id=log.id).update(status='paused')

        found = async_to_sync(_find_paused_log)(self.agent, 'agent-9-findme')

        self.assertIsNotNone(found)
        self.assertEqual(found.id, log.id)

    def test_the_lookup_does_not_touch_the_json_column(self):
        """The point of the change, pinned as SQL rather than as timing: an
        indexed equality, not a scan with a JSON extract per row."""
        from agents.agent.runtime import _find_paused_log

        with CaptureQueriesContext(connection) as captured:
            async_to_sync(_find_paused_log)(self.agent, 'agent-9-nothere')

        sql = ' '.join(q['sql'] for q in captured).lower()
        self.assertIn('thread_id', sql)
        self.assertNotIn('json_extract', sql)
        # The SELECT list always carries every column (Django fetches the
        # whole row); what matters is the filter. The WHERE clause must hit
        # the indexed column, never a JSON extract on `input_data`.
        where = sql.split('where', 1)[1] if 'where' in sql else sql
        self.assertIn('thread_id', where)
        self.assertNotIn('input_data', where)

    def test_an_over_long_thread_is_truncated_rather_than_refused(self):
        """The column is bounded and the checkpointer key is not. Truncating
        loses the lookup for a pathological id; raising loses the whole run."""
        log = self._open('x' * 400)

        self.assertEqual(len(log.thread_id), 200)
