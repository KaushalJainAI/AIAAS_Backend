"""
Regression tests for `GET /api/mcp/servers/<id>/tools/`.

The bug these exist for: the endpoint answered

    TimeoutError
    GET /api/mcp/servers/2/tools/ user=1 status=502 duration=6.423s

for *every* stdio connector — the working ones included. Three independent
faults produced that one line, and each has its own test below.

1. The budget was shorter than the work. `npx -y <pkg>` resolves and installs
   before the server prints a byte: ~8.5 s for a package that exists, ~7.7 s
   for npm to report E404 on one that does not. The endpoint waited 5 s, so a
   healthy connector and a nonexistent package were indistinguishable.

2. The reason was thrown away. `str(asyncio.TimeoutError())` is the empty
   string, so the 502 body carried `{"error": ""}`; and both transports run
   under anyio task groups, so real failures arrived as
   `ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)")` —
   structural noise with the actual cause nested inside it.

3. A disabled connection 502'd instead of being listed. `get_server_config`
   raised `PermissionDenied` for a server the user had toggled off, and the
   broad `except Exception` reported that as the server being unreachable —
   on the very page whose capability list is how a user decides whether to
   turn the connection on.
"""
import asyncio
from unittest.mock import AsyncMock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from mcp_integration.client import (
    LIST_TOOLS_TIMEOUT,
    MCPClientManager,
    MCPConnectionError,
    _describe,
    _failures,
    _get_visible_server_sync,
    _record_failure,
    _recent_failure,
    _StderrTap,
)
from mcp_integration.credential_injector import CredentialMissingError
from mcp_integration.models import MCPServer, MCPServerPreference

User = get_user_model()


def _run(coro):
    return asyncio.run(coro)


class ToolsEndpointErrorReportingTests(TestCase):
    """Every failure answers with a code and a reason a person can act on."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.server = MCPServer.objects.create(
            name='Broken Thing',
            display_name='Broken Thing',
            type='stdio',
            command='npx',
            args=['-y', '@example/does-not-exist'],
            category='utilities',
            icon_slug='broken-thing',
            user=None,
        )

    def _get_tools(self):
        return self.client.get(f'/api/mcp/servers/{self.server.id}/tools/')

    def test_timeout_answers_504_with_a_non_empty_reason(self):
        # The exact shape of the original bug: `str(TimeoutError())` is '', so
        # a body built from it alone told the user nothing at all.
        with patch.object(MCPClientManager, 'list_tools',
                          new_callable=AsyncMock, side_effect=asyncio.TimeoutError()):
            res = self._get_tools()

        self.assertEqual(res.status_code, 504)
        self.assertEqual(res.data['code'], 'connection_timeout')
        self.assertTrue(res.data['error'].strip())
        self.assertIn(f'{LIST_TOOLS_TIMEOUT:.0f}s', res.data['error'])

    def test_connection_failure_answers_502_naming_the_cause(self):
        with patch.object(MCPClientManager, 'list_tools', new_callable=AsyncMock,
                          side_effect=MCPConnectionError("npm error 404 Not Found")):
            res = self._get_tools()

        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.data['code'], 'connection_failed')
        self.assertIn('404', res.data['error'])

    def test_missing_credential_is_400_not_502(self):
        # "You have not connected an account" is the user's to fix; reporting it
        # as a bad gateway points them at the wrong thing entirely.
        with patch.object(MCPClientManager, 'list_tools', new_callable=AsyncMock,
                          side_effect=CredentialMissingError("notion", "Broken Thing")):
            res = self._get_tools()

        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data['code'], 'credential_missing')

    def test_lost_access_is_403_not_502(self):
        # `get_server_config` re-checks visibility at connect time and raises
        # Django's PermissionDenied. It used to be swallowed by the broad
        # handler and reported as the server being down.
        with patch.object(MCPClientManager, 'list_tools', new_callable=AsyncMock,
                          side_effect=PermissionDenied("not available")):
            res = self._get_tools()

        self.assertEqual(res.status_code, 403)

    def test_an_unclassified_failure_never_500s(self):
        # A third-party server failing in a way we did not anticipate is a
        # statement about that server, not about this one.
        with patch.object(MCPClientManager, 'list_tools', new_callable=AsyncMock,
                          side_effect=RuntimeError()):
            res = self._get_tools()

        self.assertEqual(res.status_code, 502)
        self.assertEqual(res.data['error'], 'RuntimeError')  # class name, never ''

    def test_success_still_returns_the_tools(self):
        tools = [{'name': 'fetch', 'description': 'Fetch a URL', 'inputSchema': {}}]
        with patch.object(MCPClientManager, 'list_tools',
                          new_callable=AsyncMock, return_value=tools):
            res = self._get_tools()

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['tools'], tools)
        self.assertEqual(res.data['server_id'], self.server.id)


class DisabledServerDiscoveryTests(TestCase):
    """Listing capabilities is how a user decides whether to enable a connection."""

    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.server = MCPServer.objects.create(
            name='Curated Thing',
            display_name='Curated Thing',
            type='stdio',
            command='npx',
            category='utilities',
            icon_slug='curated-thing',
            user=None,
        )
        MCPServerPreference.objects.create(
            user=self.user, server=self.server, enabled=False
        )

    def test_tools_are_listed_for_a_connection_the_user_turned_off(self):
        tools = [{'name': 'search', 'description': '', 'inputSchema': {}}]
        with patch.object(MCPClientManager, '_session') as session_cm:
            session = AsyncMock()
            session.list_tools = AsyncMock(return_value=type('R', (), {'tools': []})())
            session_cm.return_value.__aenter__ = AsyncMock(return_value=session)
            session_cm.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch.object(MCPClientManager, 'list_tools',
                              new_callable=AsyncMock, return_value=tools):
                res = self.client.get(f'/api/mcp/servers/{self.server.id}/tools/')

        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['tools'], tools)

    def test_discovery_relaxes_enablement_but_execution_does_not(self):
        # The gate itself, called synchronously: `get_server_config` is a thin
        # `sync_to_async` over this, and running that from a fresh event loop
        # inside a transactional TestCase deadlocks SQLite rather than testing
        # anything about enablement.
        self.assertIsNotNone(
            _get_visible_server_sync(self.server.id, self.user.id, False)
        )
        self.assertIsNone(
            _get_visible_server_sync(self.server.id, self.user.id, True)
        )


class FailureCacheTests(SimpleTestCase):
    """
    A connector that cannot start is not re-dialled on every request.

    Without this, each open of a broken card spawns another `npx` that takes
    ~8 s to fail. A user clicking through a catalogue of eleven can have a
    dozen in flight, which is how one bad row becomes a load problem.
    """

    def setUp(self):
        _failures.clear()

    def tearDown(self):
        _failures.clear()

    def test_a_recorded_failure_is_returned_without_dialling(self):
        _record_failure((7, 1), "npm error 404")
        self.assertEqual(_recent_failure((7, 1)), "npm error 404")

    def test_an_unrecorded_key_is_not_remembered(self):
        self.assertIsNone(_recent_failure((7, 1)))

    def test_a_failure_expires(self):
        import time
        _failures[(7, 1)] = (time.monotonic() - 1, "stale")
        self.assertIsNone(_recent_failure((7, 1)))
        self.assertNotIn((7, 1), _failures)


class DescribeTests(SimpleTestCase):
    """
    `_describe` exists because the two most common failures both stringify to
    something useless: an empty string, and the shape of anyio's plumbing.
    """

    def test_an_empty_message_falls_back_to_the_class_name(self):
        self.assertEqual(_describe(asyncio.TimeoutError()), 'TimeoutError')

    def test_none_is_still_a_sentence(self):
        self.assertTrue(_describe(None).strip())

    def test_an_exception_group_is_flattened_to_its_leaves(self):
        group = ExceptionGroup(
            "unhandled errors in a TaskGroup (1 sub-exception)",
            [RuntimeError("Connection closed")],
        )
        described = _describe(group)
        self.assertIn("Connection closed", described)
        self.assertNotIn("TaskGroup", described)

    def test_nested_groups_are_flattened_and_deduplicated(self):
        group = ExceptionGroup("outer", [
            ExceptionGroup("inner", [RuntimeError("boom"), RuntimeError("boom")]),
        ])
        self.assertEqual(_describe(group), "boom")

    def test_an_empty_wrapper_defers_to_its_cause(self):
        cause = RuntimeError("the actual problem")
        wrapper = RuntimeError()
        wrapper.__cause__ = cause
        self.assertEqual(_describe(wrapper), "the actual problem")


class StderrTapTests(SimpleTestCase):
    """
    The subprocess's stderr is where the diagnosis lives — npm's 404, a missing
    runtime, a rejected token. The SDK forwards it to our own stderr by default
    and the exception carries none of it.
    """

    def test_it_reports_the_first_meaningful_lines(self):
        tap = _StderrTap('Fetch')
        try:
            with open(tap.fileno(), 'w', closefd=False) as w:
                w.write(
                    "npm error code E404\n"
                    "npm error 404 Not Found - GET https://registry.npmjs.org/@x/y\n"
                    "npm error 404\n"                       # noise
                    "npm error A complete log of this run can be found in: C:\\x\n"
                )
            summary = tap.summary()
        finally:
            tap.close()

        self.assertIn('E404', summary)
        self.assertIn('Not Found', summary)
        self.assertNotIn('complete log of this run', summary)

    def test_a_silent_child_yields_no_summary(self):
        tap = _StderrTap('Quiet')
        try:
            self.assertEqual(tap.summary(), '')
        finally:
            tap.close()

    def test_the_summary_is_bounded(self):
        tap = _StderrTap('Chatty')
        try:
            with open(tap.fileno(), 'w', closefd=False) as w:
                w.write("\n".join(f"line {i} of noise" for i in range(500)))
            self.assertLessEqual(len(tap.summary()), _StderrTap._MAX_CHARS)
        finally:
            tap.close()

    def test_summary_never_raises_on_a_closed_file(self):
        # Diagnostics must never mask the error they were collected to explain.
        tap = _StderrTap('Closed')
        tap.close()
        self.assertEqual(tap.summary(), '')
