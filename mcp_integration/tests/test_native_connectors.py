"""
A connector card is not a process.

Gmail, Drive, Sheets and Calendar are `type='native'` rows (migration `0019`):
their tools are registered built-ins in `chat/tools/google/`, and the row is
what still governs them — the Connections switch, the credential, an agent's
`connectors` scope. These tests pin the two halves of that:

  * **Nothing spawns.** A native row never reaches an MCP session, is never
    listed a second time as `mcp__` tools, and stdio can be switched off for a
    whole deployment.
  * **The card still governs the tools, at both doors.** Offered only when the
    card is on and the account is connected; refused at dispatch otherwise,
    because a model names tools it saw in an earlier turn.
"""
from __future__ import annotations

from unittest.mock import patch

from asgiref.sync import async_to_sync
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from agents import connector_scope
from agents.agent.runtime import AgentToolbox, sensitive_tools_for
from chat.tools import execute_tool, get_available_tools, permissions
from credentials.models import Credential, CredentialType
from mcp_integration.client import MCPClientManager, MCPConnectionError
from mcp_integration.models import MCPServer, MCPServerPreference
from mcp_integration.tool_provider import MCPToolProvider


def connect_google(user):
    fernet = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY)
    cred = Credential(
        user=user,
        credential_type=CredentialType.objects.get(slug='google-oauth2'),
        name='Google Account',
        access_token=fernet.encrypt(b'ya29.token'),
        refresh_token=fernet.encrypt(b'1//refresh'),
        is_verified=True,
    )
    cred.set_credential_data({})
    cred.save()
    return cred


def names(descriptors):
    return {d['function']['name'] for d in descriptors}


class NothingSpawnsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='native-nospawn', password='x')
        connect_google(self.user)
        self.gmail = MCPServer.objects.get(name='Gmail', user__isnull=True)

    def test_a_native_row_refuses_to_open_a_session(self):
        manager = MCPClientManager(self.gmail.id, user=self.user)

        async def open_one():
            async with manager.connect():
                pass

        with self.assertRaises(MCPConnectionError):
            async_to_sync(open_one)()

    def test_native_tools_are_not_listed_twice_as_mcp_tools(self):
        descriptors = async_to_sync(MCPToolProvider.get_openai_tool_descriptors)(self.user)
        self.assertFalse(
            any(d['function']['name'].startswith(f'mcp__{self.gmail.id}__') for d in descriptors)
        )

    def test_listing_a_native_row_answers_from_the_registry(self):
        with patch('mcp_integration.client.MCPClientManager._session') as session:
            tools = async_to_sync(MCPClientManager(self.gmail.id, user=self.user).list_tools)()
        session.assert_not_called()
        self.assertIn('gmail_send_message', {t['name'] for t in tools})
        self.assertFalse(any(t['name'].startswith('calendar_') for t in tools))

    @override_settings(MCP_ALLOW_STDIO=False)
    def test_stdio_can_be_switched_off_for_a_deployment(self):
        row = MCPServer.objects.create(name='Local', type='stdio', command='npx', user=self.user)
        manager = MCPClientManager(row.id, user=self.user)

        async def start():
            async with manager._connect_stdio(row, None):
                pass

        # Refused before `resolve_launch` or any process call is reached.
        with patch('mcp_integration.client.resolve_launch') as launch, \
                self.assertRaises(MCPConnectionError):
            async_to_sync(start)()
        launch.assert_not_called()


class ApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='native-api', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.gmail = MCPServer.objects.get(name='Gmail', user__isnull=True)

    def test_the_tools_endpoint_lists_a_native_card(self):
        res = self.client.get(f'/api/mcp/servers/{self.gmail.id}/tools/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('gmail_search_threads', {t['name'] for t in res.data['tools']})

    def test_validation_reports_the_missing_google_connection(self):
        res = self.client.get(f'/api/mcp/servers/{self.gmail.id}/validate_credentials/')
        self.assertFalse(res.data['ok'])
        connect_google(self.user)
        from credentials.manager import get_credential_manager
        get_credential_manager().clear_cache()
        res = self.client.get(f'/api/mcp/servers/{self.gmail.id}/validate_credentials/')
        self.assertTrue(res.data['ok'], res.data)

    def test_a_user_cannot_create_a_native_row(self):
        res = self.client.post('/api/mcp/servers/', {'name': 'Mine', 'type': 'native'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('type', res.data)

    @override_settings(MCP_ALLOW_STDIO=False)
    def test_a_stdio_row_is_refused_when_stdio_is_off(self):
        res = self.client.post(
            '/api/mcp/servers/', {'name': 'Mine', 'type': 'stdio', 'command': 'npx'}, format='json',
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn('type', res.data)

    def test_a_stdio_row_is_still_accepted_where_stdio_is_on(self):
        res = self.client.post(
            '/api/mcp/servers/', {'name': 'Mine', 'type': 'stdio', 'command': 'npx'}, format='json',
        )
        self.assertEqual(res.status_code, 201, res.data)


class ChatAvailabilityTests(TestCase):
    """The card governs the tools in chat, at listing and at dispatch."""

    def setUp(self):
        self.user = User.objects.create_user(username='native-chat', password='x')
        self.gmail = MCPServer.objects.get(name='Gmail', user__isnull=True)

    def offered(self):
        return names(async_to_sync(get_available_tools)(self.user.id))

    def test_not_offered_before_google_is_connected(self):
        self.assertNotIn('gmail_search_threads', self.offered())

    def test_offered_once_google_is_connected(self):
        connect_google(self.user)
        offered = self.offered()
        self.assertIn('gmail_search_threads', offered)
        self.assertIn('calendar_list_events', offered)

    def test_switching_a_card_off_withdraws_only_its_tools(self):
        connect_google(self.user)
        MCPServerPreference.objects.create(user=self.user, server=self.gmail, enabled=False)
        offered = self.offered()
        self.assertNotIn('gmail_search_threads', offered)
        self.assertIn('calendar_list_events', offered)

    def test_dispatch_refuses_a_switched_off_card(self):
        connect_google(self.user)
        MCPServerPreference.objects.create(user=self.user, server=self.gmail, enabled=False)
        with patch('chat.tools.google.client._send') as send:
            out = async_to_sync(execute_tool)('gmail_list_labels', {}, {'user_id': self.user.id})
        self.assertIn('switched off or not', out)
        send.assert_not_called()

    def test_reads_run_ungated_in_chat_and_writes_do_not(self):
        ctx = {'user_id': self.user.id, 'session_id': 's'}
        self.assertFalse(async_to_sync(permissions.default_policy)('gmail_search_threads', {}, ctx))
        self.assertTrue(async_to_sync(permissions.default_policy)('gmail_send_message', {}, ctx))

    def test_unattended_runs_gate_even_a_mailbox_read(self):
        ctx = {'user_id': self.user.id, 'session_id': 's'}
        self.assertTrue(async_to_sync(permissions.unattended_policy)('gmail_search_threads', {}, ctx))


class AgentScopeTests(TestCase):
    """The `mcp` grant and the `connectors` scope govern native tools too."""

    def setUp(self):
        self.user = User.objects.create_user(username='native-agent', password='x')
        connect_google(self.user)
        self.gmail = MCPServer.objects.get(name='Gmail', user__isnull=True)
        self.calendar = MCPServer.objects.get(name='Google Calendar', user__isnull=True)

    def box(self, connectors=None, **kwargs):
        return AgentToolbox(
            grants=kwargs.pop('grants', {'mcp': True}), user_id=self.user.id,
            mcp_scope=None if connectors is None else connector_scope.parse(connectors),
            **kwargs,
        )

    def offered(self, box):
        return names(async_to_sync(box.descriptors)())

    def test_the_grant_is_required(self):
        box = self.box(grants={})
        self.assertNotIn('gmail_search_threads', self.offered(box))
        out = async_to_sync(box.dispatch)('gmail_search_threads', {}, {'user_id': self.user.id})
        self.assertIn('not available to this agent', out)

    def test_a_scope_naming_calendar_withholds_gmail_at_both_doors(self):
        box = self.box([self.calendar.id])
        offered = self.offered(box)
        self.assertIn('calendar_list_events', offered)
        self.assertNotIn('gmail_search_threads', offered)
        with patch('chat.tools.google.client._send') as send:
            out = async_to_sync(box.dispatch)('gmail_search_threads', {}, {'user_id': self.user.id})
        self.assertIn('not available to this agent', out)
        send.assert_not_called()

    def test_read_mode_uses_the_declared_effect(self):
        box = self.box([{'id': self.gmail.id, 'mode': 'read'}])
        offered = self.offered(box)
        self.assertIn('gmail_search_threads', offered)
        self.assertNotIn('gmail_send_message', offered)
        _, refusal = async_to_sync(box.native_call_allowed)('gmail_send_message')
        self.assertIn('only read', refusal)

    def test_selected_mode_offers_only_what_it_names(self):
        box = self.box([{'id': self.gmail.id, 'mode': 'selected', 'tools': ['gmail_get_thread']}])
        offered = self.offered(box)
        self.assertIn('gmail_get_thread', offered)
        self.assertNotIn('gmail_search_threads', offered)

    def test_plan_keeps_the_reads_and_drops_the_writes(self):
        box = self.box(read_only=True)
        offered = self.offered(box)
        self.assertIn('gmail_search_threads', offered)
        self.assertNotIn('gmail_send_message', offered)

    def test_auto_autonomy_still_stops_before_a_send(self):
        gated = sensitive_tools_for('auto', self.box())
        self.assertIn('gmail_send_message', gated)
        self.assertIn('calendar_create_event', gated)
        self.assertNotIn('gmail_trash_message', gated)
