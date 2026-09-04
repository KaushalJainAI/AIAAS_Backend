"""
The tools chat gained after the registry lost its file and code capabilities:
the sandboxed `execute_python`, and the three that reach the agent runtime.

The ownership tests here are the point of the file. Every one of these tools is
reachable directly through `/api/chat/execute-tool/` with attacker-chosen
arguments — the model is not the only caller — so "the agent would never ask for
someone else's id" is not a control. Each one is asserted from the other user's
side.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase

from agents.models import SubAgent

from chat.models import ChatAttachment, ChatSession
from chat.tools import AVAILABLE_TOOLS, SENSITIVE_TOOLS, all_tools, execute_tool


def run(name: str, args: dict, user) -> str:
    return async_to_sync(execute_tool)(name, args, {"user_id": user.id})


class UserFixture(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        User = get_user_model()
        cls.owner = User.objects.create_user(
            email="owner@example.com", username="owner", password="pw"
        )
        cls.other = User.objects.create_user(
            email="other@example.com", username="other", password="pw"
        )


# ─────────────────────────────────────────────────────────────────────────
# execute_python
# ─────────────────────────────────────────────────────────────────────────

class SandboxToolTests(UserFixture):
    def test_arithmetic_comes_back_as_result(self):
        payload = json.loads(run("execute_python", {"code": "result = sum(range(101))"}, self.owner))
        self.assertEqual(payload["result"], 5050)

    def test_stdout_is_captured_alongside_result(self):
        payload = json.loads(
            run("execute_python", {"code": 'print("hi")\nresult = {"a": 1}'}, self.owner)
        )
        self.assertEqual(payload["stdout"].strip(), "hi")
        self.assertEqual(payload["result"], {"a": 1})

    def test_unserialisable_result_degrades_to_repr(self):
        # The transcript only carries JSON; a set must not fail the whole call.
        payload = json.loads(run("execute_python", {"code": "result = {1, 2}"}, self.owner))
        self.assertIn("1", payload["result"])

    def test_the_escapes_it_exists_to_block(self):
        for code in (
            'import os\nresult = os.listdir(".")',
            'open("/etc/passwd").read()',
            '__import__("socket")',
            "import subprocess",
        ):
            with self.subTest(code=code):
                self.assertTrue(run("execute_python", {"code": code}, self.owner).startswith("Error"))

    def test_runtime_error_is_reported_not_raised(self):
        self.assertTrue(run("execute_python", {"code": "1/0"}, self.owner).startswith("Error"))

    def test_empty_code_is_rejected_rather_than_run(self):
        # Wording is load-bearing: orchestrator's toolbox dispatches straight
        # through to here and its tests assert on this string.
        self.assertIn("'code' is required", run("execute_python", {"code": "   "}, self.owner))

    def test_failures_are_plain_text_not_json(self):
        # A model that json.loads() a traceback would read it as a result.
        result = run("execute_python", {"code": "import os"}, self.owner)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(result)


# ─────────────────────────────────────────────────────────────────────────
# search_agents / run_agent / get_agent_run
# ─────────────────────────────────────────────────────────────────────────

class AgentToolTests(UserFixture):
    def setUp(self) -> None:
        self.agent = SubAgent.objects.create(
            user=self.owner,
            name="Inbox Triage",
            description="Sorts and summarises incoming mail every morning",
            tags=["email", "daily"],
            tool_grants={"webSearch": True, "codeExecution": True, "shell": False},
            guardrails={"autonomy": "ask"},
        )
        self.foreign = SubAgent.objects.create(
            user=self.other, name="Someone Elses Agent"
        )

    def test_listing_returns_only_this_users_agents(self):
        payload = json.loads(run("search_agents", {}, self.owner))
        self.assertEqual([a["name"] for a in payload["agents"]], ["Inbox Triage"])

    def test_archived_agents_are_hidden(self):
        SubAgent.objects.filter(id=self.agent.id).update(status="archived")
        self.assertEqual(json.loads(run("search_agents", {}, self.owner))["agents"], [])

    def test_terms_match_across_name_description_and_tags(self):
        for query in ("inbox", "summarise mail", "daily"):
            with self.subTest(query=query):
                self.assertEqual(json.loads(run("search_agents", {"query": query}, self.owner))["count"], 1)

    def test_only_granted_tools_are_reported(self):
        agent = json.loads(run("search_agents", {}, self.owner))["agents"][0]
        self.assertEqual(agent["granted_tools"], ["codeExecution", "webSearch"])
        self.assertEqual(agent["autonomy"], "ask")

    def test_no_match_says_so_rather_than_returning_something_else(self):
        # An empty list plus an instruction not to invent an id: a model handed
        # a bare [] tends to guess one.
        payload = json.loads(run("search_agents", {"query": "zzzz nothing"}, self.owner))
        self.assertEqual(payload["agents"], [])
        self.assertIn("Do not invent", payload["message"])

    def test_limit_is_clamped(self):
        payload = json.loads(run("search_agents", {"limit": 9999}, self.owner))
        self.assertLessEqual(len(payload["agents"]), 25)

    def test_another_users_agent_cannot_be_run(self):
        payload = json.loads(run("run_agent", {"agent_id": self.agent.id, "goal": "go"}, self.other))
        self.assertIn("error", payload)

    def test_bad_arguments_are_refused_before_anything_starts(self):
        self.assertIn("numeric id", run("run_agent", {"agent_id": "abc", "goal": "g"}, self.owner))
        self.assertIn("'goal' is required", run("run_agent", {"agent_id": self.agent.id, "goal": " "}, self.owner))

    def test_run_agent_requires_approval(self):
        # It spends the user's budget and acts under grants they cannot see from
        # here, so it must reach the HITL gate rather than run on model say-so.
        self.assertIn("run_agent", SENSITIVE_TOOLS)

    def test_a_malformed_execution_id_does_not_raise(self):
        payload = json.loads(run("get_agent_run", {"execution_id": "not-a-uuid"}, self.owner))
        self.assertIn("error", payload)

    def test_another_users_run_is_not_readable(self):
        from logs.models import ExecutionLog

        log = ExecutionLog.objects.create(subagent=self.foreign, user=self.other, status="completed",
            output_data={"answer": "private answer"},
        )
        payload = json.loads(run("get_agent_run", {"execution_id": str(log.execution_id)}, self.owner))
        self.assertIn("error", payload)
        self.assertNotIn("private answer", json.dumps(payload))

    def test_owner_reads_their_finished_run(self):
        from logs.models import ExecutionLog

        log = ExecutionLog.objects.create(subagent=self.agent, user=self.owner, status="completed",
            output_data={"answer": "42"}, tokens_used=7,
        )
        payload = json.loads(run("get_agent_run", {"execution_id": str(log.execution_id)}, self.owner))
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["answer"], "42")

    def test_a_paused_run_is_reported_not_waited_on(self):
        from logs.models import ExecutionLog

        log = ExecutionLog.objects.create(subagent=self.agent, user=self.owner, status="paused",
        )
        payload = json.loads(run("get_agent_run", {"execution_id": str(log.execution_id)}, self.owner))
        self.assertEqual(payload["status"], "paused")

    def test_a_still_running_run_reports_running_without_blocking(self):
        from logs.models import ExecutionLog

        log = ExecutionLog.objects.create(subagent=self.agent, user=self.owner, status="running",
        )
        payload = json.loads(run("get_agent_run", {"execution_id": str(log.execution_id)}, self.owner))
        self.assertEqual(payload["status"], "running")
        self.assertIn("do not start the agent again", payload["message"])


# ─────────────────────────────────────────────────────────────────────────
# Ownership regressions
# ─────────────────────────────────────────────────────────────────────────

class AttachmentOwnershipTests(UserFixture):
    """`read_attachment_text` used to check ownership through `message`.

    An upload creates the attachment before any message references it, and the
    FK is SET_NULL, so `message` is null for essentially every row — which made
    `if att.message and ...` pass unconditionally. Any authenticated user could
    read any attachment's extracted text by id, directly through
    `/api/chat/execute-tool/`.
    """

    def setUp(self) -> None:
        self.session = ChatSession.objects.create(user=self.owner, title="private")
        self.attachment = ChatAttachment.objects.create(
            session=self.session, filename="secret.txt", file_type="text",
            extracted_text="CONFIDENTIAL", file="secret.txt",
        )

    def test_owner_can_read_it(self):
        payload = json.loads(
            run("read_attachment_text", {"attachment_id": str(self.attachment.id)}, self.owner)
        )
        self.assertEqual(payload["content"], "CONFIDENTIAL")

    def test_another_user_cannot(self):
        result = run("read_attachment_text", {"attachment_id": str(self.attachment.id)}, self.other)
        self.assertIn("Access denied", result)
        self.assertNotIn("CONFIDENTIAL", result)

    def test_message_is_null_on_a_fresh_upload(self):
        # The premise of the test above: if this ever stops being true the old
        # guard would start working and this regression would look fixed.
        self.assertIsNone(self.attachment.message)


class ToolRegistryTests(TestCase):
    def test_generate_image_is_neither_advertised_nor_dispatchable(self):
        # It was advertised with no dispatch entry, so a model that called it got
        # "not recognized" — an offer the registry could not honour.
        names = {t["function"]["name"] for t in AVAILABLE_TOOLS}
        self.assertNotIn("generate_image", names)
        User = get_user_model()
        user = User.objects.create_user(email="r@example.com", username="r", password="pw")
        self.assertIn("not recognized", run("generate_image", {"prompt": "x"}, user))

    def test_every_advertised_tool_can_be_dispatched(self):
        """
        The gap generate_image fell through, closed for the whole registry.

        This used to scrape the source of the dispatch function for each name,
        because the schema list and the dispatch table were separate objects
        that could disagree. They are now the same registration, so this asserts
        the invariant rather than inspecting text for it.
        """
        for entry in all_tools():
            with self.subTest(tool=entry.name):
                self.assertEqual(entry.schema["function"]["name"], entry.name)
                self.assertTrue(callable(entry.run))

        self.assertEqual(
            {t["function"]["name"] for t in AVAILABLE_TOOLS},
            {entry.name for entry in all_tools()},
        )
