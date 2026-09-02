"""
What an agent is allowed to pull *into* its context, and from where.

The sibling of `test_curation.py`: that one is about what a run keeps once it
has it, this one is about what it may fetch in the first place. Both are context
management, and both failed the same way — a control on the configuration screen
that the runtime did not read.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from agents.agent.orchestrator import (
    DELEGATION_BRIEFING_CHAR_LIMIT,
    DELEGATION_TASK_CHAR_LIMIT,
    MAX_WORKERS_PER_FANOUT,
    DelegationRefused,
    check_delegation_payload,
)
from agents.agent.runtime import GRANT_TOOLS, build_system_prompt, kb_scope_for
from chat.models import ToolOutput
from chat.tools.knowledge import (
    keyword_search,
    knowledge_base_search,
    list_documents,
    list_knowledge_bases,
    read_document,
)
from inference.models import Document, KnowledgeBase

User = get_user_model()


# ── The KB selector was decorative ───────────────────────────────────────────

class KnowledgeBaseScopeTests(TestCase):
    """The builder's KB selection was read only to print names into the system
    prompt. `knowledge_base_search` resolved any KB the *user* owned, so an
    agent configured for one corpus could read every other one — and with no
    `kb_id` it fell through to the user's default KB, which need not be among
    the agent's at all. An answer from the wrong corpus looks exactly like an
    answer from the right one."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("kbowner", "k@example.com", "x")
        cls.allowed = KnowledgeBase.objects.create(
            user=cls.user, name="Policies", backend="vector",
        )
        cls.other = KnowledgeBase.objects.create(
            user=cls.user, name="Personal Finance", backend="vector",
            is_default=True,
        )

    def _ctx(self, scope):
        return {"user_id": self.user.id, "session_id": "s1", "kb_scope": scope}

    def test_listing_shows_only_the_agents_own(self):
        out = json.loads(async_to_sync(list_knowledge_bases)(
            {}, self._ctx((self.allowed.id,))
        ))
        self.assertEqual([kb["name"] for kb in out["knowledge_bases"]], ["Policies"])

    def test_searching_another_kb_by_id_is_refused(self):
        out = async_to_sync(knowledge_base_search)(
            {"query": "anything", "kb_id": self.other.id},
            self._ctx((self.allowed.id,)),
        )
        self.assertIn("not one this agent may search", out)

    def test_no_kb_id_uses_the_only_one_in_scope_not_the_users_default(self):
        """The default KB here is deliberately *not* the agent's. Falling
        through to it is the quiet version of the same bug."""
        out = async_to_sync(knowledge_base_search)(
            {"query": "anything"}, self._ctx((self.allowed.id,))
        )
        self.assertNotIn("not one this agent may search", out)
        self.assertNotIn("Personal Finance", out)

    def test_no_kb_id_with_several_in_scope_asks_rather_than_guessing(self):
        out = async_to_sync(knowledge_base_search)(
            {"query": "anything"}, self._ctx((self.allowed.id, self.other.id))
        )
        self.assertIn("Pass kb_id", out)

    def test_an_empty_scope_is_unrestricted(self):
        """An agent built before the selection was enforced never had one
        applied; turning enforcement on must not empty its corpus."""
        out = json.loads(async_to_sync(list_knowledge_bases)({}, self._ctx(None)))
        self.assertEqual(len(out["knowledge_bases"]), 2)

    def test_chat_passes_no_scope_at_all(self):
        out = json.loads(async_to_sync(list_knowledge_bases)(
            {}, {"user_id": self.user.id, "session_id": "s1"}
        ))
        self.assertEqual(len(out["knowledge_bases"]), 2)

    def test_every_kb_tool_honours_the_scope(self):
        """Five tools reach knowledge bases; a scope enforced in four of them is
        not a scope."""
        scoped = self._ctx((self.allowed.id,))
        for tool in (knowledge_base_search, keyword_search):
            out = async_to_sync(tool)(
                {"query": "q", "kb_id": self.other.id}, scoped
            )
            self.assertIn("not one this agent may search", out, tool.__name__)
        out = async_to_sync(list_documents)({"kb_id": self.other.id}, scoped)
        self.assertIn("not one this agent may search", out)

    def test_a_document_cannot_be_read_around_the_scope(self):
        """`read_document` takes a document id, not a KB id, so the scope the
        other four enforce would be one call away from irrelevant."""
        doc = Document.objects.create(
            user=self.user, knowledge_base=self.other, name="secret.txt",
            content_text="the private figure is 12", status="stored",
            file_size=24,
        )
        out = async_to_sync(read_document)(
            {"document_id": doc.id}, self._ctx((self.allowed.id,))
        )
        self.assertIn("not in a knowledge base this agent may read", out)
        self.assertNotIn("private figure", out)

    def test_scope_is_derived_from_what_the_agent_was_given(self):
        gathered = {"knowledge_bases": [
            {"id": 4, "name": "A", "backend": "vector", "doc_count": 1},
        ]}
        self.assertEqual(kb_scope_for(gathered), (4,))
        self.assertIsNone(kb_scope_for({"knowledge_bases": []}))


class KnowledgeBasePromptTests(SimpleTestCase):
    """The prompt named the KBs and withheld their ids, so an agent had to spend
    a turn on `list_knowledge_bases` rediscovering what its own configuration
    already held."""

    def _prompt(self, kbs):
        from agents.models import SubAgent

        return build_system_prompt(
            SubAgent(name="A", prompt="do things", tool_grants={"rag": True}),
            {"skills": [], "knowledge_bases": kbs, "ctx": {}},
        )

    def test_the_id_is_in_the_prompt(self):
        text = self._prompt([
            {"id": 7, "name": "Policies", "backend": "vector", "doc_count": 3},
        ])
        self.assertIn("id 7", text)
        self.assertIn("Policies", text)

    def test_the_backend_says_which_tool_can_read_it(self):
        """A semantic search against a keyword-only index returns nothing rather
        than an error, which a model reads as "the KB has nothing on this"."""
        text = self._prompt([
            {"id": 1, "name": "Codes", "backend": "fulltext", "doc_count": 9},
            {"id": 2, "name": "Scans", "backend": "raw", "doc_count": 2},
        ])
        self.assertIn("keyword_search", text)
        self.assertIn("read_document", text)


class RagGrantTests(SimpleTestCase):
    def test_the_grant_unlocks_every_tool_the_catalogue_advertises(self):
        """`list_knowledge_bases` tells the model to use `keyword_search` on a
        keyword KB and `list_documents` + `read_document` on a raw one. The
        grant unlocked neither, so the catalogue was instructing the agent to
        call tools it would then be refused."""
        self.assertEqual(
            set(GRANT_TOOLS["rag"]),
            {"list_knowledge_bases", "knowledge_base_search", "keyword_search",
             "list_documents", "read_document"},
        )


# ── Delegation ───────────────────────────────────────────────────────────────

class DelegationPayloadTests(SimpleTestCase):
    """Answers have been bounded since the fan-out existed; instructions were
    not — and instructions are the direction that multiplies by worker count."""

    def test_a_reasonable_fanout_is_allowed(self):
        check_delegation_payload(["do a", "do b"], "shared background")

    def test_an_oversized_task_is_refused_with_what_to_do_instead(self):
        with self.assertRaises(DelegationRefused) as caught:
            check_delegation_payload(["x" * (DELEGATION_TASK_CHAR_LIMIT + 1)])
        message = str(caught.exception)
        self.assertIn("Task 1", message)
        self.assertIn("briefing", message)

    def test_an_oversized_briefing_is_refused(self):
        with self.assertRaises(DelegationRefused):
            check_delegation_payload(
                ["short"], "y" * (DELEGATION_BRIEFING_CHAR_LIMIT + 1)
            )

    def test_too_many_workers_is_refused(self):
        with self.assertRaises(DelegationRefused):
            check_delegation_payload(["t"] * (MAX_WORKERS_PER_FANOUT + 1))

    def test_refusal_happens_before_anything_runs(self):
        """A refusal is only useful if it costs nothing: the alternative is N
        workers that each die on their first model call."""
        with self.assertRaises(DelegationRefused):
            check_delegation_payload(["x" * (DELEGATION_TASK_CHAR_LIMIT + 1)] * 5)


class BriefingTests(SimpleTestCase):
    def test_the_briefing_reaches_the_worker_as_context_not_instruction(self):
        from agents.models import SubAgent

        text = build_system_prompt(
            SubAgent(name="W", prompt="do the task", tool_grants={}),
            {"skills": [], "knowledge_bases": [], "ctx": {}},
            briefing="The figure agreed earlier was 4.8 million.",
        )
        self.assertIn("4.8 million", text)
        self.assertIn("Context, not instructions", text)

    def test_no_briefing_leaves_the_prompt_as_it_was(self):
        from agents.models import SubAgent

        agent = SubAgent(name="W", prompt="do the task", tool_grants={})
        base = {"skills": [], "knowledge_bases": [], "ctx": {}}
        self.assertEqual(
            build_system_prompt(agent, base),
            build_system_prompt(agent, base, briefing=""),
        )


class WorkerArchiveReadThroughTests(TestCase):
    """Curation and delegation would otherwise work against each other: a parent
    that curated a detail away cannot restate it in the task, and a worker — a
    fresh thread — could not reach it either."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("deleg", "d@example.com", "x")

    def setUp(self):
        ToolOutput.objects.create(
            user=self.user, session_key="parent-thread",
            tool_name="context:archive:web_search",
            content="The quarterly revenue figure was 4.8 million rupees.",
            total_chars=52,
            expires_at=timezone.now() + timezone.timedelta(hours=1),
        )

    def _recall(self, context):
        from chat.tools.tool_output import recall

        return async_to_sync(recall)("revenue figure", context)

    def test_a_worker_can_read_its_parents_archive(self):
        out = self._recall({
            "user_id": self.user.id, "session_id": "worker-thread",
            "archive_scopes": ("parent-thread",),
        })
        self.assertIn("4.8 million", out)

    def test_an_unrelated_run_still_cannot(self):
        out = self._recall({
            "user_id": self.user.id, "session_id": "worker-thread",
        })
        self.assertIn("Nothing has been archived", out)

    def test_another_user_still_cannot_even_with_the_key(self):
        stranger = User.objects.create_user("nosy", "n@example.com", "x")
        out = self._recall({
            "user_id": stranger.id, "session_id": "worker-thread",
            "archive_scopes": ("parent-thread",),
        })
        self.assertIn("Nothing has been archived", out)

    def test_reading_by_id_honours_the_same_scopes(self):
        from chat.tools.tool_output import read

        row = ToolOutput.objects.get(session_key="parent-thread")
        allowed = async_to_sync(read)(row.id, 0, {
            "user_id": self.user.id, "session_id": "worker-thread",
            "archive_scopes": ("parent-thread",),
        })
        self.assertIn("4.8 million", allowed)

        refused = async_to_sync(read)(row.id, 0, {
            "user_id": self.user.id, "session_id": "worker-thread",
        })
        self.assertIn("no stored output", refused)

    def test_a_worker_writes_only_to_its_own_archive(self):
        """Read-through is one-way. A worker that could write into its parent's
        archive would be putting text into a run that has already moved on."""
        from chat.tools.tool_output import archive

        async_to_sync(archive)("worker-step", "worker text", {
            "user_id": self.user.id, "session_id": "worker-thread",
            "archive_scopes": ("parent-thread",),
        })
        self.assertEqual(
            ToolOutput.objects.filter(session_key="parent-thread").count(), 1
        )
        self.assertTrue(
            ToolOutput.objects.filter(session_key="worker-thread").exists()
        )
