"""
What the agent loop pays for before every model call, and what it stops paying.

`agent_node` rebuilds the tool list on every pass of the loop. Most of that list
cannot change while a run is going, but one part of it is expensive in a way
nothing local can fix: resolving MCP descriptors is a database read per
connection, a Redis read, and — whenever the 120-second cache has lapsed, which
any conversation with a pause in it will — an `npx` subprocess start bounded at
eight seconds. All of it lands in front of the first token.

So the MCP half is memoised per turn and the built-in half is not. These tests
pin both halves of that split, because getting it wrong in either direction is
silent: memoise nothing and every iteration pays the connectors again, memoise
everything and a tool the run has just earned is withheld for the rest of it.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from chat.models import ToolOutput
from chat.tools import get_available_tools


def _names(tools) -> set[str]:
    return {t.get("function", {}).get("name") for t in tools}


_FAKE_MCP = [{
    "type": "function",
    "function": {"name": "mcp__1__fetch", "description": "x", "parameters": {}},
}]


class McpMemoTests(TestCase):
    """The expensive half is resolved once per turn."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="memo", email="m@example.test", password="x"
        )

    def _resolve(self, memo, calls):
        async def _fake(user_id, *args, **kwargs):
            calls.append(user_id)
            return list(_FAKE_MCP)

        with patch(
            "mcp_integration.tool_provider.MCPToolProvider.get_openai_tool_descriptors",
            new=_fake,
        ):
            return async_to_sync(get_available_tools)(
                self.user.id, session_key="sess-1", mcp_memo=memo,
            )

    def test_a_shared_memo_resolves_connectors_once(self):
        memo: dict = {}
        calls: list = []
        for _ in range(4):
            tools = self._resolve(memo, calls)
            # Every pass still *offers* the MCP tools; only the resolution is
            # skipped. A memo that dropped them would be a different bug.
            self.assertIn("mcp__1__fetch", _names(tools))
        self.assertEqual(len(calls), 1, "connectors were re-resolved mid-turn")

    def test_without_a_memo_nothing_is_remembered(self):
        calls: list = []
        for _ in range(3):
            self._resolve(None, calls)
        self.assertEqual(len(calls), 3)

    def test_separate_turns_do_not_share_a_memo(self):
        calls: list = []
        self._resolve({}, calls)
        self._resolve({}, calls)
        self.assertEqual(len(calls), 2, "a memo outlived the turn that owned it")

    def test_a_failed_resolution_is_not_remembered(self):
        """A cold subprocess that timed out must not cost the whole run.

        Remembering the failure would be the worst of both: the turn keeps the
        latency it already paid and loses every connector for the rest of the
        run, with no way back until the user sends another message.
        """
        memo: dict = {}
        attempts: list = []

        async def _boom(user_id, *args, **kwargs):
            attempts.append(user_id)
            raise TimeoutError("cold npx")

        with patch(
            "mcp_integration.tool_provider.MCPToolProvider.get_openai_tool_descriptors",
            new=_boom,
        ):
            for _ in range(3):
                tools = async_to_sync(get_available_tools)(
                    self.user.id, session_key="sess-1", mcp_memo=memo,
                )
                # Degraded, not broken: the built-ins are all still there.
                self.assertIn("web_search", _names(tools))
        self.assertEqual(len(attempts), 3)


class MemoDoesNotFreezeRequirementsTests(TestCase):
    """The half that genuinely moves is still rebuilt every pass.

    `read_tool_output` becomes real the moment a run spills an oversized
    result, which happens *during* the loop. Freezing the whole list at the
    first iteration would withhold it for the rest of the run, and the notice
    naming it — written by `tool_output.bound` — would be pointing at a tool
    the model cannot see.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="unfrozen", email="u@example.test", password="x"
        )

    def test_a_tool_earned_mid_turn_is_offered_on_the_next_pass(self):
        memo: dict = {}
        first = async_to_sync(get_available_tools)(
            self.user.id, session_key="sess-9", mcp_memo=memo,
        )
        self.assertNotIn("read_tool_output", _names(first))

        ToolOutput.objects.create(
            user=self.user, session_key="sess-9", tool_name="mcp_fetch",
            content="...", total_chars=3,
            expires_at=timezone.now() + timedelta(hours=1),
        )

        second = async_to_sync(get_available_tools)(
            self.user.id, session_key="sess-9", mcp_memo=memo,
        )
        self.assertIn("read_tool_output", _names(second))
