"""
The per-call approval gate, and the hole it was written to close.

`SENSITIVE_TOOLS` is a list of built-in names. MCP tool names are minted at
runtime from a third-party server's catalogue, so that list could never contain
one — while `credential_injector` hands those same calls the user's real keys.
The result was `mcp__<id>__send_email_<digest>` running against the user's
mailbox with no gate, and in unattended agent runs with nobody watching either.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from chat.tools import permissions
from chat.models import ToolPermission
from mcp_integration.models import MCPServer
from mcp_integration.tool_provider import encode_tool_name


class ReadOnlyNameTests(SimpleTestCase):
    """The allowlist is prefix-and-word-boundary, and closed by default."""

    def test_common_read_verbs_are_recognised(self):
        for name in ("get_user", "list-messages", "search_files", "read.file",
                     "fetch_page", "query", "describe_table", "status_check"):
            self.assertTrue(permissions.looks_read_only(name), name)

    def test_write_verbs_are_not(self):
        for name in ("send_email", "create_issue", "delete_file", "update_row",
                     "post_message", "transfer_funds", "revoke_token"):
            self.assertFalse(permissions.looks_read_only(name), name)

    def test_a_read_verb_must_end_on_a_word_boundary(self):
        # Otherwise "getaway_book" and "listen_and_delete" read as safe.
        self.assertFalse(permissions.looks_read_only("getaway_book"))
        self.assertFalse(permissions.looks_read_only("listen_and_delete"))

    def test_an_unknown_verb_is_treated_as_a_write(self):
        # The asymmetry is the whole design: an extra prompt costs a click,
        # a missed one sends the email.
        self.assertFalse(permissions.looks_read_only("frobnicate_the_ledger"))
        self.assertFalse(permissions.looks_read_only(""))


class PolicyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="gated", email="g@example.test", password="x"
        )
        self.credentialed = MCPServer.objects.create(
            name="Mailbox", url="https://mcp.example.test/mail",
            required_credential_types=["google_oauth"],
        )
        self.plain = MCPServer.objects.create(
            name="Public Docs", url="https://mcp.example.test/docs",
        )
        self.context = {"user_id": self.user.id, "session_id": "sess-1"}

    def _call(self, server, tool):
        return encode_tool_name(server.id, tool)

    def _gated(self, name, policy=None):
        policy = policy or permissions.default_policy
        return async_to_sync(policy)(name, {}, self.context)

    # ── chat ──

    def test_builtin_tools_are_left_to_the_name_list(self):
        self.assertFalse(self._gated("web_search"))
        self.assertFalse(self._gated("execute_python"))

    def test_a_credentialed_write_is_gated(self):
        self.assertTrue(self._gated(self._call(self.credentialed, "send_email")))

    def test_a_credentialed_read_is_not(self):
        self.assertFalse(self._gated(self._call(self.credentialed, "list_messages")))

    def test_an_uncredentialed_write_is_not_gated(self):
        # Nothing of the user's is at stake; it acts with no authority at all.
        self.assertFalse(self._gated(self._call(self.plain, "create_note")))

    def test_an_unresolvable_server_is_treated_as_credentialed(self):
        # A lookup failure must not silently downgrade a gated call.
        self.assertTrue(self._gated("mcp__999999__send_email_abcd1234"))

    # ── unattended runs ──

    def test_unattended_runs_gate_reads_too(self):
        name = self._call(self.credentialed, "list_messages")
        self.assertFalse(self._gated(name))
        self.assertTrue(self._gated(name, permissions.unattended_policy))

    def test_unattended_runs_still_ignore_uncredentialed_tools(self):
        self.assertFalse(
            self._gated(self._call(self.plain, "create_note"),
                        permissions.unattended_policy)
        )

    def test_full_autonomy_gates_nothing(self):
        self.assertFalse(
            self._gated(self._call(self.credentialed, "send_email"),
                        permissions.never)
        )

    # ── remembered decisions ──

    def test_a_standing_allowance_stops_the_prompt(self):
        name = self._call(self.credentialed, "send_email")
        self.assertTrue(self._gated(name))

        ToolPermission.objects.create(user=self.user, tool_name=name, session_key="")
        self.assertFalse(self._gated(name))

    def test_a_session_scoped_allowance_does_not_leak(self):
        name = self._call(self.credentialed, "send_email")
        ToolPermission.objects.create(
            user=self.user, tool_name=name, session_key="sess-1"
        )
        self.assertFalse(self._gated(name))

        other = {"user_id": self.user.id, "session_id": "sess-2"}
        self.assertTrue(async_to_sync(permissions.default_policy)(name, {}, other))

    def test_one_users_allowance_does_not_cover_another(self):
        name = self._call(self.credentialed, "send_email")
        ToolPermission.objects.create(user=self.user, tool_name=name, session_key="")

        stranger = get_user_model().objects.create_user(
            username="stranger", email="s@example.test", password="x"
        )
        self.assertTrue(
            async_to_sync(permissions.default_policy)(
                name, {}, {"user_id": stranger.id, "session_id": "sess-1"}
            )
        )

    def test_a_guest_cannot_carry_a_standing_allowance(self):
        # There is no account to key it on, so a stored decision would apply to
        # every guest at once.
        name = self._call(self.credentialed, "send_email")
        self.assertTrue(
            async_to_sync(permissions.default_policy)(
                name, {}, {"user_id": None, "session_id": "sess-1"}
            )
        )


class AutonomyWiringTests(SimpleTestCase):
    """The runtime picks the policy from the agent's autonomy setting."""

    def test_full_opts_out(self):
        from agents.agent.runtime import approval_policy_for

        self.assertIs(approval_policy_for('full'), permissions.never)

    def test_everything_else_gets_the_unattended_policy(self):
        from agents.agent.runtime import approval_policy_for

        for autonomy in ('ask', 'review', ''):
            self.assertIs(
                approval_policy_for(autonomy), permissions.unattended_policy, autonomy
            )
