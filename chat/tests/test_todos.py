"""
The run's plan: what it holds, and the one property it exists for.

A long run forgets its own goal. `curate_node` folds the oldest part of the
transcript into a summary note, and the oldest part is where the original
instruction lives — so at iteration 30 the model is working from a compressed
trace of its own footprints with no statement of intent anywhere. The plan is
the fix, and it only works if two things hold: curation cannot reach it, and
the model is shown it again every turn. Both are pinned below, because both
fail silently — a plan that gets curated away and a plan nothing reads back
look identical from outside (a model that quietly loses the thread).
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from chat.turn import todos
from chat.tools.planning import update_todos


def _plan(*pairs):
    return [{"text": t, "status": s} for t, s in pairs]


class NormalizeTests(SimpleTestCase):
    def test_a_bare_string_is_an_open_item(self):
        """Forgiving on shape: the model meant an item, so give it one."""
        self.assertEqual(todos.normalize(["find the docs"]),
                         [{"text": "find the docs", "status": "open"}])

    def test_an_unknown_status_becomes_open(self):
        """Strict on vocabulary, and it must not be kept as sent.

        An item in a status nothing recognises would count as neither finished
        nor outstanding, so it would sit in the list for ever without either
        being worked on or blocking completion.
        """
        self.assertEqual(todos.normalize([{"text": "x", "status": "in_progress"}]),
                         [{"text": "x", "status": "open"}])

    def test_alternate_keys_are_accepted(self):
        for key in ("text", "task", "title"):
            with self.subTest(key=key):
                self.assertEqual(todos.normalize([{key: "step"}])[0]["text"], "step")

    def test_empty_and_junk_entries_are_dropped(self):
        self.assertEqual(todos.normalize([{"text": "  "}, 42, None, ["x"]]), [])

    def test_not_a_list_is_empty(self):
        self.assertEqual(todos.normalize("do the thing"), [])

    def test_the_list_is_capped(self):
        out = todos.normalize([f"step {i}" for i in range(todos.MAX_TODOS + 10)])
        self.assertEqual(len(out), todos.MAX_TODOS)

    def test_an_item_is_capped(self):
        out = todos.normalize(["x" * (todos.MAX_TODO_CHARS + 100)])
        self.assertEqual(len(out[0]["text"]), todos.MAX_TODO_CHARS)


class UnfinishedTests(SimpleTestCase):
    def test_open_and_doing_count_as_unfinished(self):
        plan = _plan(("a", "open"), ("b", "doing"), ("c", "done"))
        self.assertEqual([t["text"] for t in todos.unfinished(plan)], ["a", "b"])

    def test_blocked_counts_as_settled(self):
        """Blocked is a reported failure, which is the outcome to encourage.

        If blocked counted as unfinished, a model told not to stop with open
        work would mark things done to escape the loop — turning the list from
        a record into a lie. An honest blocker is worth more than a false
        completion.
        """
        self.assertEqual(todos.unfinished(_plan(("a", "blocked"))), [])


class RenderTests(SimpleTestCase):
    def test_nothing_renders_for_an_empty_plan(self):
        """A run with no plan must pay nothing for the feature."""
        self.assertEqual(todos.render([]), "")

    def test_open_items_are_listed_and_done_ones_are_counted(self):
        """Re-reading finished work every turn is paid for every turn.

        The count still has to be there: without it the model cannot tell
        progress from a fresh start, and re-plans work it already did.
        """
        out = todos.render(_plan(("wrote it", "done"), ("ship it", "open")))
        self.assertIn("ship it", out)
        self.assertNotIn("wrote it", out)
        self.assertIn("1 step(s) done", out)

    def test_a_finished_plan_says_to_answer(self):
        out = todos.render(_plan(("a", "done"), ("b", "done")))
        self.assertIn("All 2 steps are done", out)

    def test_blocked_items_stay_visible(self):
        # They are not open work, but they are what the final answer has to
        # account for, so they must not vanish from the model's view.
        out = todos.render(_plan(("no api key", "blocked")))
        self.assertIn("no api key", out)
        self.assertIn("blocked", out)


class ToolTests(SimpleTestCase):
    def _call(self, **args):
        return json.loads(async_to_sync(update_todos)(args, {}))

    def test_it_echoes_what_was_actually_stored(self):
        """Not "ok" — the model has to see what survived normalisation."""
        out = self._call(todos=[{"text": "a", "status": "doing"},
                                {"text": "b", "status": "nonsense"}])
        self.assertEqual(out["todos"],
                         _plan(("a", "doing"), ("b", "open")))
        self.assertEqual(out["open"], 2)

    def test_dropping_items_is_reported(self):
        """Silently keeping 20 of 25 would leave the model planning around five
        steps it believes are tracked and are not."""
        out = self._call(todos=[f"s{i}" for i in range(todos.MAX_TODOS + 5)])
        self.assertIn("note", out)
        self.assertIn("dropped", out["note"])

    def test_an_empty_plan_is_an_error_not_a_silent_wipe(self):
        out = self._call(todos=[])
        self.assertIn("error", out)


class GraphIntegrationTests(SimpleTestCase):
    """The two properties the feature actually rests on."""

    def test_the_plan_is_stored_in_graph_state_not_in_the_transcript(self):
        """`metadata` is returned by `tools_node` and kept by the checkpointer.

        Living there rather than in `messages` is what makes the plan immune to
        curation *by construction*: `curate_node` rewrites messages and nothing
        else, so there is no list of exclusions for anyone to forget to update.
        """
        from chat.turn.agent import _SIDE_EFFECTS

        self.assertIn("update_todos", _SIDE_EFFECTS)

        meta: dict = {}
        seen: list = []

        async def sink(event, payload):
            seen.append((event, payload))

        async_to_sync(_SIDE_EFFECTS["update_todos"])(
            {"type": "todos", "todos": _plan(("a", "open"))}, {}, meta, sink,
        )
        self.assertEqual(meta["todos"], _plan(("a", "open")))
        self.assertTrue(seen, "the client is never told the plan changed")

    def test_curation_only_ever_rewrites_messages(self):
        """The structural claim above, asserted rather than assumed.

        If `curate_node` ever starts returning other state keys, a plan parked
        in `metadata` stops being safe and this test is where that shows up.
        """
        import inspect

        from chat.turn import agent as agent_mod

        source = inspect.getsource(agent_mod.curate_node)
        self.assertIn('"messages"', source)
        self.assertNotIn('"metadata"', source)

    def test_the_plan_is_never_folded_into_the_system_prompt(self):
        """It changes most turns; the system prompt is the cached prefix.

        This is the clock trap (`prompts.build_context_update`) — folding a
        per-turn value into the baseline costs the whole session's prefix
        caching, on every call, and reports nothing.
        """
        import inspect

        from chat.turn import agent as agent_mod, prompts

        self.assertNotIn("todos", inspect.getsource(prompts.build_system_message))
        # It rides as a trailing system message in history instead.
        source = inspect.getsource(agent_mod.agent_node)
        self.assertIn("todos.render", source)
        self.assertIn('history.append', source)
