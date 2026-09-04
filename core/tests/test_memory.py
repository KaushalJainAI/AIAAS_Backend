"""
Durable facts about a user, and what keeps the store from rotting.

Personalisation had no substrate before this: `UserProfile` holds billing,
`ChatSession.memory_enabled` replays one conversation, and nothing answered
"what do I know about this person". So an assistant told to personalise could
only re-derive it from the current transcript — the very context curation folds
away on a long run and discards entirely between sessions.

The risk a memory store carries is not that it fails; it is that it *fills*.
Every fact here rides in the system prompt of every future turn, so a store
full of "the user said hello" costs money for ever and buries the three facts
that matter. Most of what is pinned below is about that: dedupe, per-category
caps, eviction by recency of use, and a hard ceiling on what reaches a prompt.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from core import memory
from core.models import UserMemory


class RememberTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="rememberer", email="r@example.test", password="x"
        )

    def test_a_fact_is_stored_once(self):
        row, created = memory.remember(self.user, "Prefers concise answers",
                                       "preference")
        self.assertTrue(created)
        self.assertEqual(row.category, "preference")

    def test_the_same_fact_twice_is_one_row(self):
        """Otherwise the store fills with the same thing in slightly different
        words each time a conversation revisits it."""
        memory.remember(self.user, "Works in IST", "profile")
        _, created = memory.remember(self.user, "Works in IST", "profile")

        self.assertFalse(created)
        self.assertEqual(UserMemory.objects.filter(user=self.user).count(), 1)

    def test_a_repeat_refreshes_recency(self):
        """A fact that keeps coming up is evidently worth keeping.

        Eviction is by least-recently-touched, so without this a fact confirmed
        in every conversation would still age out behind a one-off.

        The stamp is pushed into the past first rather than compared against a
        second `remember` taken microseconds later — two writes in the same
        clock tick make an ordering assertion pass or fail by chance.
        """
        from datetime import timedelta

        from django.utils import timezone

        memory.remember(self.user, "Uses Django", "project")
        stale = timezone.now() - timedelta(days=1)
        UserMemory.objects.filter(user=self.user).update(updated_at=stale)

        memory.remember(self.user, "Uses Django", "project")
        self.assertGreater(UserMemory.objects.get(user=self.user).updated_at, stale)

    def test_an_unknown_category_falls_back_rather_than_failing(self):
        row, _ = memory.remember(self.user, "Something", "wildly-invented")
        self.assertEqual(row.category, "context")

    def test_an_empty_fact_stores_nothing(self):
        row, created = memory.remember(self.user, "   ")
        self.assertIsNone(row)
        self.assertFalse(created)
        self.assertEqual(UserMemory.objects.count(), 0)

    def test_the_cap_is_per_category(self):
        """A burst of project facts must not evict who the user is.

        The categories are not competing for one budget — they answer different
        questions, and a cap shared between them would let the noisiest kind of
        fact push out the most useful.
        """
        memory.remember(self.user, "Is a backend engineer", "profile")
        for i in range(UserMemory.MAX_PER_CATEGORY + 5):
            memory.remember(self.user, f"Project fact {i}", "project")

        self.assertEqual(
            UserMemory.objects.filter(user=self.user, category="project").count(),
            UserMemory.MAX_PER_CATEGORY,
        )
        self.assertTrue(
            UserMemory.objects.filter(user=self.user, category="profile").exists()
        )

    def test_eviction_drops_the_least_recently_used(self):
        """Recency of *use*, not of creation — which is what makes a repeated
        fact durable and a one-off disposable.

        Timestamps are set explicitly rather than left to the loop: the writes
        all land inside the same microsecond, so real ordering among them would
        be arbitrary and this test would pass or fail by chance.
        """
        from datetime import timedelta

        from django.utils import timezone

        now = timezone.now()
        for i in range(UserMemory.MAX_PER_CATEGORY):
            memory.remember(self.user, f"fact {i}", "context")
            # `update()` bypasses `auto_now`, which `save()` would overwrite.
            UserMemory.objects.filter(user=self.user, text=f"fact {i}").update(
                updated_at=now - timedelta(hours=100 - i)
            )

        # `fact 0` is the oldest, and touching it makes it the newest.
        memory.remember(self.user, "fact 0", "context")
        memory.remember(self.user, "one more", "context")

        surviving = set(
            UserMemory.objects.filter(user=self.user).values_list("text", flat=True)
        )
        self.assertEqual(len(surviving), UserMemory.MAX_PER_CATEGORY)
        self.assertIn("fact 0", surviving)
        self.assertIn("one more", surviving)
        self.assertNotIn("fact 1", surviving)


class ForgetTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="forgetter", email="f@example.test", password="x"
        )
        memory.remember(self.user, "Wrong fact", "context")

    def test_forgetting_removes_the_row(self):
        self.assertEqual(memory.forget(self.user, "Wrong fact"), 1)
        self.assertFalse(UserMemory.objects.filter(user=self.user).exists())

    def test_forgetting_is_case_insensitive(self):
        # The model is quoting a fact back from a prompt it rendered; requiring
        # an exact-case match would make the undo fail for cosmetic reasons.
        self.assertEqual(memory.forget(self.user, "wrong FACT"), 1)

    def test_forgetting_something_unknown_removes_nothing(self):
        self.assertEqual(memory.forget(self.user, "never said this"), 0)
        self.assertEqual(UserMemory.objects.filter(user=self.user).count(), 1)


class ForPromptTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="prompted", email="p@example.test", password="x"
        )

    def test_no_memories_render_nothing(self):
        """A user we know nothing about must pay nothing for the feature."""
        self.assertEqual(memory.for_prompt(self.user.id), "")

    def test_no_user_renders_nothing(self):
        self.assertEqual(memory.for_prompt(None), "")

    def test_facts_are_grouped_by_category(self):
        memory.remember(self.user, "Is a backend engineer", "profile")
        memory.remember(self.user, "Prefers code first", "preference")

        block = memory.for_prompt(self.user.id)
        self.assertIn("Is a backend engineer", block)
        self.assertIn("Prefers code first", block)
        self.assertIn("Who they are", block)
        self.assertIn("How they like to work", block)

    def test_the_block_is_bounded_and_cut_on_whole_facts(self):
        """Half a sentence about someone reads as a fact of its own.

        The block rides in every system prompt, so it needs a ceiling — but
        truncating mid-fact could state something flatly untrue about the user,
        which is worse than omitting it.
        """
        for i in range(200):
            memory.remember(self.user, f"Fact number {i} " + "x" * 100, "context")

        block = memory.for_prompt(self.user.id)
        self.assertLessEqual(len(block), memory.MAX_PROMPT_CHARS)
        for line in block.splitlines():
            if line.startswith("- "):
                self.assertTrue(line.endswith("x"), f"cut mid-fact: {line[-30:]!r}")

    def test_another_users_memories_are_never_included(self):
        other = get_user_model().objects.create_user(
            username="stranger", email="s@example.test", password="x"
        )
        memory.remember(other, "Secret about someone else", "profile")
        memory.remember(self.user, "My own fact", "profile")

        block = memory.for_prompt(self.user.id)
        self.assertIn("My own fact", block)
        self.assertNotIn("Secret about someone else", block)


class PromptWiringTests(TestCase):
    """Where the block lands, which is the part that is easy to get wrong."""

    def test_it_goes_in_the_baseline_not_the_per_turn_update(self):
        """Session-stable is the bar for the cached prefix, not immutable.

        Memory changes only when a fact is written; the clock changed on every
        turn, which is why that one had to move out. Putting memory in the
        per-turn update instead would be harmless for caching but wrong in
        meaning — it is standing knowledge, not a bulletin about this turn.
        """
        from chat.models import ChatSession
        from chat.turn import prompts

        user = get_user_model().objects.create_user(
            username="wired", email="w@example.test", password="x"
        )
        session = ChatSession.objects.create(user=user, title="t")

        block = "### WHAT YOU KNOW ABOUT THIS USER ###\n- Likes tea"
        self.assertIn(
            "Likes tea", prompts.build_system_message(session, user_memory=block)
        )
        self.assertNotIn(
            "Likes tea",
            prompts.build_context_update(session, "now", "chat"),
        )

    def test_an_agent_run_reads_memory_but_cannot_write_it(self):
        """A scheduled run is personalised, and cannot rewrite the person.

        The memory tools are chat's alone: they are in no `GRANT_TOOLS` value
        and not in `ALWAYS_AVAILABLE`, so an unattended run cannot quietly
        change what the platform believes about someone.
        """
        from agents.agent.runtime import ALWAYS_AVAILABLE, GRANT_TOOLS

        granted = {n for names in GRANT_TOOLS.values() for n in names}
        granted |= set(ALWAYS_AVAILABLE)
        self.assertNotIn("remember_about_user", granted)
        self.assertNotIn("forget_about_user", granted)
