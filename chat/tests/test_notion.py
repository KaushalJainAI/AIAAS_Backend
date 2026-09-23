"""The native Notion connector tools, against a fake wire and a real card.

Pins both halves of the native contract: the tools speak the Notion API
correctly (search, page read with clipping, database rows, page creation),
and the Connections card governs them — offered only when the card is on
and the token stored, refused at dispatch otherwise.
"""
from __future__ import annotations

import json
from unittest import mock

from asgiref.sync import async_to_sync
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase

from chat.tools import execute_tool, get_available_tools, permissions
from chat.tools import notion
from credentials.models import Credential, CredentialType
from mcp_integration.models import MCPServer, MCPServerPreference

CTX = {"user_id": None, "session_id": "s", "turn_id": "t"}


def connect_notion(user):
    cred = Credential(
        user=user,
        credential_type=CredentialType.objects.get(slug='notion'),
        name='Notion',
        is_verified=True,
    )
    cred.set_credential_data({'token': 'ntn_x'})
    cred.save()
    return cred


class FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


class FakeClient:
    """Routes (method, path fragment) to canned responses."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for route_method, fragment, response in self.routes:
            if route_method == method and fragment in url:
                return response() if callable(response) else response
        raise AssertionError(f'unexpected {method} {url}')


def run(tool, args, routes, user_id=7):
    client = FakeClient(routes)
    ctx = dict(CTX)
    ctx['user_id'] = user_id
    with mock.patch(
            'workflow_backend.httpclient.shared_client', return_value=client):
        return async_to_sync(tool)(args, ctx), client


def parsed(out: str) -> dict:
    return json.loads(out)


def _authed(user_id=7):
    user = User.objects.create_user(f'notion-{user_id}', 'n@example.com', 'pw')
    connect_notion(user)
    return user


class SearchTests(TestCase):
    def test_lists_pages_and_databases_with_titles(self):
        user = _authed()
        out, client = run(notion.notion_search, {'query': 'road'}, [
            ('POST', '/search', FakeResp(200, {'results': [
                {'id': 'p1', 'object': 'page', 'url': 'https://n/p1',
                 'properties': {'Name': {'type': 'title', 'title': [
                     {'plain_text': 'Roadmap'}]}}},
                {'id': 'd1', 'object': 'database', 'url': 'https://n/d1',
                 'title': [{'plain_text': 'Bugs'}]},
            ]})),
        ], user_id=user.id)
        found = parsed(out)
        self.assertEqual(found['count'], 2)
        self.assertEqual(found['results'][0]['title'], 'Roadmap')
        self.assertEqual(found['results'][1], {
            'id': 'd1', 'kind': 'database', 'title': 'Bugs', 'url': 'https://n/d1'})
        method, url, kwargs = client.calls[0]
        self.assertIn('Bearer ntn_x', kwargs['headers']['Authorization'])
        self.assertEqual(kwargs['headers']['Notion-Version'], '2022-06-28')

    def test_missing_token_names_the_fix(self):
        out, _ = run(notion.notion_search, {}, [('POST', '/search', FakeResp())])
        body = parsed(out)
        self.assertEqual(body['code'], 'credential_missing')
        self.assertIn('Connections', body['error'])


class ReadTests(TestCase):
    BLOCKS = {'results': [
        {'type': 'heading_1', 'heading_1': {'rich_text': [{'plain_text': 'Hi'}]}},
        {'type': 'paragraph', 'paragraph': {'rich_text': [{'plain_text': 'Body'}]}},
    ]}
    PAGE = {'id': 'p1', 'url': 'https://n/p1',
            'properties': {'Name': {'type': 'title', 'title': [{'plain_text': 'Hi'}]}}}

    def test_reads_title_props_and_text(self):
        user = _authed()
        out, _ = run(notion.notion_read_page, {'page_id': 'p1'}, [
            ('GET', '/pages/p1', FakeResp(200, dict(self.PAGE))),
            ('GET', '/blocks/p1/children', FakeResp(200, dict(self.BLOCKS))),
        ], user_id=user.id)
        body = parsed(out)
        self.assertEqual(body['title'], 'Hi')
        self.assertEqual(body['text'], 'Hi\nBody')
        self.assertNotIn('truncated', body)

    def test_long_pages_are_clipped_with_a_notice(self):
        user = _authed()
        big = {'results': [
            {'type': 'paragraph',
             'paragraph': {'rich_text': [{'plain_text': 'x' * 20000}]}}]}
        out, _ = run(notion.notion_read_page, {'page_id': 'p1'}, [
            ('GET', '/pages/p1', FakeResp(200, dict(self.PAGE))),
            ('GET', '/blocks/p1/children', FakeResp(200, big)),
        ], user_id=user.id)
        body = parsed(out)
        self.assertTrue(body['truncated'])
        self.assertIn('clipped', body['text'])

    def test_unshared_id_says_to_share_first(self):
        user = _authed()
        out, _ = run(notion.notion_read_page, {'page_id': 'nope'}, [
            ('GET', '/pages/nope', FakeResp(404, {'message': 'Could not find page'})),
        ], user_id=user.id)
        body = parsed(out)
        self.assertEqual(body['code'], 'not_found')
        self.assertIn('Share it', body['error'])


class QueryTests(TestCase):
    def test_rows_come_back_plain(self):
        user = _authed()
        rows_payload = {'results': [{
            'id': 'r1',
            'properties': {
                'Name': {'type': 'title', 'title': [{'plain_text': 'Bug'}]},
                'N': {'type': 'number', 'number': 3},
                'Done': {'type': 'checkbox', 'checkbox': True},
            },
        }]}
        out, _ = run(notion.notion_query_database, {'database_id': 'd1'}, [
            ('POST', '/databases/d1/query', FakeResp(200, rows_payload)),
        ], user_id=user.id)
        rows = parsed(out)['rows']
        self.assertEqual(rows[0]['values'],
                         {'Name': 'Bug', 'N': 3, 'Done': True})


class CreateTests(TestCase):
    def test_creates_under_a_parent_with_title_and_text(self):
        user = _authed()
        out, client = run(notion.notion_create_page,
                          {'parent_page_id': 'p0', 'title': 'T', 'text': 'A\n\nB'}, [
                              ('POST', '/pages', FakeResp(200, {
                                  'id': 'p9', 'url': 'https://n/p9'})),
                          ], user_id=user.id)
        body = parsed(out)
        self.assertEqual(body['id'], 'p9')
        _, _, kwargs = client.calls[0]
        payload = kwargs['json']
        self.assertEqual(payload['parent'], {'page_id': 'p0'})
        self.assertEqual(len(payload['children']), 2)

    def test_create_is_sensitive_and_irreversible(self):
        from chat.tools.registry import get

        tool = get('notion_create_page')
        self.assertTrue(tool.sensitive)
        self.assertEqual(tool.effect, 'irreversible')
        self.assertEqual(get('notion_search').effect, 'read')


class CardGovernanceTests(TestCase):
    """The Connections card governs the tools, at listing and at dispatch."""

    def setUp(self):
        self.user = User.objects.create_user(username='native-notion', password='x')
        self.card = MCPServer.objects.get(name='Notion', user__isnull=True)

    def offered(self):
        return {d['function']['name']
                for d in async_to_sync(get_available_tools)(self.user.id)}

    def test_not_offered_before_notion_is_connected(self):
        self.assertNotIn('notion_search', self.offered())

    def test_offered_once_notion_is_connected(self):
        connect_notion(self.user)
        offered = self.offered()
        self.assertIn('notion_search', offered)
        self.assertIn('notion_read_page', offered)
        self.assertIn('notion_query_database', offered)
        self.assertIn('notion_create_page', offered)

    def test_switching_the_card_off_withdraws_its_tools(self):
        connect_notion(self.user)
        MCPServerPreference.objects.create(
            user=self.user, server=self.card, enabled=False)
        offered = self.offered()
        self.assertNotIn('notion_search', offered)

    def test_dispatch_refuses_a_switched_off_card(self):
        connect_notion(self.user)
        MCPServerPreference.objects.create(
            user=self.user, server=self.card, enabled=False)
        out = async_to_sync(execute_tool)(
            'notion_search', {}, {'user_id': self.user.id})
        self.assertIn('switched off or not', out)

    def test_reads_run_ungated_in_chat_and_create_does_not(self):
        ctx = {'user_id': self.user.id, 'session_id': 's'}
        self.assertFalse(async_to_sync(permissions.default_policy)(
            'notion_search', {}, ctx))
        self.assertTrue(async_to_sync(permissions.default_policy)(
            'notion_create_page', {}, ctx))


class CatalogueTests(TestCase):
    def test_the_row_is_native_and_asks_for_a_token(self):
        row = MCPServer.objects.get(name='Notion', user__isnull=True)
        self.assertEqual(row.type, 'native')
        self.assertTrue(row.enabled)
        self.assertEqual(row.required_credential_types, ['notion'])
        self.assertFalse(row.coming_soon)
