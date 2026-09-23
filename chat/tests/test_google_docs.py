"""The native Google Docs tools, against a fake wire.

Reads keep structure (headings, lists, tables) instead of arriving as flat
export text; writes go through documents.create/batchUpdate behind the
sensitive + irreversible gate.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from chat.tools.google import client, docs

CTX = {"user_id": None, "session_id": "s", "turn_id": "t"}


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


class Wire:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for route_method, fragment, response in self.routes:
            if route_method == method and fragment in url:
                if isinstance(response, list):
                    return response.pop(0)
                return response
        raise AssertionError(f"unexpected {method} {url}")


def run(tool, args, wire):
    with patch.object(client, "_send", new=wire), \
         patch.object(client, "_access_token", new=AsyncMock(return_value="tok")), \
         patch.object(client.asyncio, "sleep", new=AsyncMock()):
        return async_to_sync(tool)(args, CTX)


def parsed(out: str) -> dict:
    return json.loads(out)


DOC = {
    'documentId': 'd1', 'title': 'Plan',
    'body': {'content': [
        {'paragraph': {
            'paragraphStyle': {'namedStyleType': 'HEADING_1'},
            'elements': [{'textRun': {'content': 'Goals\n'}}]}},
        {'paragraph': {
            'paragraphStyle': {'namedStyleType': 'NORMAL_TEXT'},
            'elements': [{'textRun': {'content': 'Ship it\n'}}]}},
        {'table': {'tableRows': [{'tableCells': [
            {'content': [{'paragraph': {
                'paragraphStyle': {},
                'elements': [{'textRun': {'content': 'a'}}]}}]},
            {'content': [{'paragraph': {
                'paragraphStyle': {},
                'elements': [{'textRun': {'content': 'b'}}]}}]},
        ]}]}},
    ]},
}


class ReadTests(SimpleTestCase):
    def test_keeps_headings_and_tables(self):
        out = run(docs.docs_read, {'document_id': 'd1'},
                  Wire([('GET', '/documents/d1', _json(200, DOC))]))
        body = parsed(out)
        self.assertEqual(body['title'], 'Plan')
        self.assertIn('# Goals', body['text'])
        self.assertIn('a | b', body['text'])
        self.assertNotIn('truncated', body)

    def test_long_documents_are_clipped_with_a_notice(self):
        big = {'documentId': 'd1', 'title': 'Big',
               'body': {'content': [{'paragraph': {
                   'paragraphStyle': {},
                   'elements': [{'textRun': {'content': 'x' * 30000}}]}}]}}
        out = run(docs.docs_read, {'document_id': 'd1'},
                  Wire([('GET', '/documents/d1', _json(200, big))]))
        body = parsed(out)
        self.assertTrue(body['truncated'])
        self.assertIn('clipped', body['text'])

    def test_an_id_is_required(self):
        body = parsed(run(docs.docs_read, {}, Wire([])))
        self.assertEqual(body['code'], 'tool_error')


class CreateTests(SimpleTestCase):
    def test_creates_and_links(self):
        out = run(docs.docs_create, {'title': 'Notes'}, Wire([
            ('POST', '/documents',
             _json(200, {'documentId': 'd9', 'title': 'Notes'})),
        ]))
        body = parsed(out)
        self.assertEqual(body['id'], 'd9')
        self.assertEqual(
            body['url'], 'https://docs.google.com/document/d/d9/edit')

    def test_create_is_sensitive_and_irreversible(self):
        from chat.tools.registry import get

        self.assertTrue(get('docs_create').sensitive)
        self.assertEqual(get('docs_create').effect, 'irreversible')
        self.assertEqual(get('docs_read').effect, 'read')


class AppendTests(SimpleTestCase):
    END = {'body': {'content': [{}, {'endIndex': 11}]}}

    def test_appends_before_the_final_newline(self):
        wire = Wire([
            ('GET', '/documents/d1', _json(200, dict(self.END))),
            ('POST', '/documents/d1:batchUpdate', _json(200, {})),
        ])
        out = run(docs.docs_append,
                  {'document_id': 'd1', 'text': 'More.'}, wire)
        body = parsed(out)
        self.assertEqual(body['characters'], len('More.'))
        _, _, kwargs = wire.calls[1]
        request = kwargs['json']['requests'][0]['insertText']
        self.assertEqual(request['location'], {'index': 10})

    def test_more_than_twenty_paragraphs_is_refused(self):
        wire = Wire([
            ('GET', '/documents/d1', _json(200, dict(self.END))),
            ('POST', '/documents/d1:batchUpdate', _json(200, {})),
        ])
        text = '\n\n'.join(f'p{i}' for i in range(25))
        out = run(docs.docs_append, {'document_id': 'd1', 'text': text}, wire)
        body = parsed(out)
        _, _, kwargs = wire.calls[1]
        inserted = kwargs['json']['requests'][0]['insertText']['text']
        self.assertIn('refused', inserted)
        self.assertEqual(body['characters'], len(inserted) - 1)
