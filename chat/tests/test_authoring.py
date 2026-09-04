"""
Building an agent by describing it, and the two walls around that.

An agent config accepted from a model is only safe because `AgentSerializer`
checks that every id in it belongs to the caller. So the tool writes through
that serializer rather than touching `SubAgent` directly — a second write path
is a second place for those checks to be missing, and here they are the whole
safety story.

The two containment properties are the rest of it, and both are invisible from
the tool schema:

* an agent run cannot reach these tools at all, so a delegating agent cannot
  mint a worker holding grants it was itself refused;
* the call is `sensitive`, so a person approves the configuration — grants
  included — before anything is written.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase

from agents.models import SubAgent
from chat.tools.authoring import create_agent, update_agent


class CreateAgentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="author", email="a@example.test", password="x"
        )
        self.ctx = {"user_id": self.user.id}

    def _create(self, **args):
        return json.loads(async_to_sync(create_agent)(args, self.ctx))

    def test_an_agent_is_created_with_the_grants_it_asked_for(self):
        out = self._create(
            name="Inbox Triage", brief="Sort the inbox each morning.",
            tools={"webSearch": True},
        )
        agent = SubAgent.objects.get(id=out["agent_id"])
        self.assertEqual(agent.user, self.user)
        self.assertTrue(agent.tool_grants["webSearch"])

    def test_ungranted_capabilities_are_stored_as_denied_not_absent(self):
        """`apply` writes the whole closed set for a reason.

        An absent key would read as "unset, so whatever the runtime defaults
        to", which is exactly how a permissions screen stops meaning anything.
        """
        out = self._create(name="Reader", brief="Read things.",
                           tools={"webSearch": True})
        grants = SubAgent.objects.get(id=out["agent_id"]).tool_grants
        self.assertIs(grants["mcp"], False)
        self.assertIs(grants["subAgents"], False)

    def test_a_duplicate_name_is_suffixed_rather_than_failing(self):
        first = self._create(name="Daily", brief="b")
        second = self._create(name="Daily", brief="b")
        self.assertNotEqual(first["agent_id"], second["agent_id"])
        self.assertEqual(second["name"], "Daily (1)")

    def test_a_knowledge_base_the_user_does_not_own_is_refused(self):
        """The check that makes this safe to accept from a model at all."""
        out = self._create(name="Snoop", brief="b", knowledgeBases=[99999])
        self.assertIn("error", out)
        self.assertFalse(SubAgent.objects.filter(name="Snoop").exists())

    def test_a_nonexistent_capability_is_refused_by_the_serializer(self):
        out = self._create(name="Wishful", brief="b", tools={"telepathy": True})
        self.assertIn("error", out)

    def test_fields_outside_the_writable_set_are_ignored(self):
        """A model has no basis for choosing a spend cap.

        Letting it set one means either a surprise bill or a run that dies
        half-way; both are worse than the default it does not get to touch.
        """
        out = self._create(name="Thrifty", brief="b", spendCapRupees=999999)
        agent = SubAgent.objects.get(id=out["agent_id"])
        self.assertNotEqual((agent.guardrails or {}).get("spendCapRupees"), 999999)

    def test_a_creation_revision_is_recorded(self):
        """Otherwise the agent's first edit looks like it invented everything."""
        from logs.models import SubAgentRevision

        out = self._create(name="Tracked", brief="b")
        self.assertTrue(
            SubAgentRevision.objects.filter(subagent_id=out["agent_id"]).exists()
        )

    def test_a_schedule_is_actually_armed(self):
        """Accepting a cron expression and never arming it is the worst
        outcome: the user is told their agent runs daily and it never does."""
        from agents.models import Trigger

        out = self._create(name="Nightly", brief="b", schedule="0 9 * * *",
                           scheduleTimezone="Asia/Kolkata", allowUnattended=True)
        trigger = Trigger.objects.filter(subagent_id=out["agent_id"]).first()
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.origin, "builder")
        self.assertEqual(trigger.timezone, "Asia/Kolkata")

    def test_a_schedule_without_unattended_clearance_is_refused(self):
        """The two are validated as a pair, and that is what makes it safe to
        let a model set either.

        A schedule on an agent not cleared to run unattended is not a
        half-configured agent — it is one whose every firing the runtime
        refuses. Because the serializer couples them, a user approving a
        scheduled agent is necessarily approving unattended runs, so the
        consent cannot be split from the consequence.
        """
        out = self._create(name="Half", brief="b", schedule="0 9 * * *")
        self.assertIn("error", out)
        self.assertIn("allowUnattended", out.get("details", {}))
        self.assertFalse(SubAgent.objects.filter(name="Half").exists())

    def test_unattended_is_off_unless_asked_for(self):
        """Every row starts closed; nothing may open it by omission."""
        out = self._create(name="Manual", brief="b")
        agent = SubAgent.objects.get(id=out["agent_id"])
        self.assertFalse((agent.guardrails or {}).get("allowUnattended", False))

    def test_no_user_in_context_writes_nothing(self):
        out = json.loads(async_to_sync(create_agent)({"name": "x", "brief": "y"}, {}))
        self.assertIn("error", out)
        self.assertEqual(SubAgent.objects.count(), 0)


class UpdateAgentTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="editor", email="e@example.test", password="x"
        )
        self.ctx = {"user_id": self.user.id}
        created = json.loads(async_to_sync(create_agent)(
            {"name": "Base", "brief": "original brief",
             "tools": {"webSearch": True}}, self.ctx,
        ))
        self.agent_id = created["agent_id"]

    def _update(self, **args):
        return json.loads(async_to_sync(update_agent)(
            {"agent_id": self.agent_id, **args}, self.ctx,
        ))

    def test_an_unsent_field_keeps_its_value(self):
        """PATCH semantics, and for a grant the alternative is silent widening
        or narrowing of what an agent may do."""
        self._update(description="now described")
        agent = SubAgent.objects.get(id=self.agent_id)
        self.assertEqual(agent.prompt, "original brief")
        self.assertTrue(agent.tool_grants["webSearch"])

    def test_sending_tools_replaces_the_whole_set(self):
        # Stated in the tool description too, because a model that assumes
        # merge semantics would silently revoke a capability it did not mention.
        self._update(tools={"rag": True})
        grants = SubAgent.objects.get(id=self.agent_id).tool_grants
        self.assertTrue(grants["rag"])
        self.assertFalse(grants["webSearch"])

    def test_another_users_agent_cannot_be_touched(self):
        other = get_user_model().objects.create_user(
            username="stranger", email="s@example.test", password="x"
        )
        theirs = SubAgent.objects.create(user=other, name="Theirs", prompt="p")

        out = json.loads(async_to_sync(update_agent)(
            {"agent_id": theirs.id, "brief": "hijacked"}, self.ctx,
        ))
        self.assertIn("error", out)
        theirs.refresh_from_db()
        self.assertEqual(theirs.prompt, "p")

    def test_a_missing_id_is_an_error_not_a_create(self):
        out = json.loads(async_to_sync(update_agent)({"brief": "x"}, self.ctx))
        self.assertIn("error", out)


class ContainmentTests(TestCase):
    """The two walls. Both are silent when broken, so both get a test."""

    def test_an_agent_run_cannot_reach_the_authoring_tools(self):
        """Otherwise every grant becomes a suggestion.

        An agent holding `subAgents` could otherwise create a *new* agent with
        the grants it was refused and delegate to it. Depth and budget bounds
        would still hold; permission bounds would not.
        """
        from agents.agent.runtime import (
            ALWAYS_AVAILABLE, GRANT_TOOLS, RETRIEVAL_TOOLS,
        )

        reachable = {n for names in GRANT_TOOLS.values() for n in names}
        reachable |= set(ALWAYS_AVAILABLE) | set(RETRIEVAL_TOOLS)
        self.assertNotIn("create_agent", reachable)
        self.assertNotIn("update_agent", reachable)

    def test_the_toolbox_refuses_them_at_dispatch_too(self):
        """Not advertising a tool is not access control — a model will name one
        it saw in an earlier transcript."""
        from agents.agent.runtime import AgentToolbox

        box = AgentToolbox(grants={"subAgents": True, "mcp": True}, user_id=1)
        out = async_to_sync(box.dispatch)("create_agent", {}, {"user_id": 1})
        self.assertIn("not", out.lower())
        self.assertEqual(SubAgent.objects.count(), 0)

    def test_they_are_sensitive_so_a_person_approves_the_grants(self):
        """The model proposes capabilities; a person grants them.

        That is what makes model-chosen grants acceptable at all, and it reuses
        the existing approval gate rather than inventing a second confirmation
        flow that could disagree with it.
        """
        from chat.tools import SENSITIVE_TOOLS

        self.assertIn("create_agent", SENSITIVE_TOOLS)
        self.assertIn("update_agent", SENSITIVE_TOOLS)
