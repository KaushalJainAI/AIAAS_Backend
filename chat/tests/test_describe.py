"""
Turning a tool call into a sentence.

Four surfaces used to improvise from the raw name and the raw arguments, which
is why the Inbox asked people to approve `mcp__7__send_email_ab12cd34` without
naming a recipient and the chat card printed `JSON.stringify(args)`. The tests
below are mostly about the refusals: what a description must never carry onto
an approval screen, and what it must never claim.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from chat.tools.describe import (
    LONG_VALUE_CHARS, MAX_FIELDS, describe_call, describe_call_async,
)


def _values(detail: dict) -> list[str]:
    return [f['value'] for f in detail['fields']]


def _labels(detail: dict) -> list[str]:
    return [f['label'] for f in detail['fields']]


class NamingTests(SimpleTestCase):
    def test_an_mcp_call_loses_its_prefix_and_digest(self):
        detail = describe_call('mcp__7__send_email_ab12cd34', {}, server='Gmail')

        self.assertEqual(detail['tool'], 'send_email')
        self.assertEqual(detail['title'], 'Send email · Gmail')
        self.assertIn('Gmail', detail['sentence'])
        # The encoded form must not survive into anything a person reads.
        self.assertNotIn('mcp__', detail['title'])
        self.assertNotIn('ab12cd34', detail['sentence'])

    def test_an_unnamed_connection_still_describes_the_call(self):
        """Degrades rather than fails.

        The server label needs a database row, and a description that fell
        over when that read failed would take the approval screen with it.
        """
        detail = describe_call('mcp__7__send_email_ab12cd34', {})

        self.assertEqual(detail['tool'], 'send_email')
        self.assertEqual(detail['title'], 'Send email')
        self.assertIn('connected account', detail['sentence'])

    def test_a_builtin_gets_a_written_phrase_where_one_exists(self):
        self.assertEqual(describe_call('write_file', {})['title'], 'Save a file')
        # And humanises acceptably where one does not.
        self.assertEqual(describe_call('list_documents', {})['title'],
                         'List documents')

    def test_an_unrecognisable_name_never_raises(self):
        for name in ('', 'mcp__', 'mcp__notanumber__x', '___', 'mcp__9__'):
            with self.subTest(name=name):
                detail = describe_call(name, {'a': 1})
                self.assertIsInstance(detail['title'], str)
                self.assertTrue(detail['title'])


class FieldTests(SimpleTestCase):
    def test_secrets_are_replaced_not_shown(self):
        detail = describe_call('mcp__7__post_ab12cd34', {
            'url': 'https://example.test/hook',
            'api_key': 'sk-live-abcdef',
            'Authorization': 'Bearer nope',
            'refresh_token': 'rt-1',
        })

        joined = ' '.join(_values(detail))
        for leaked in ('sk-live-abcdef', 'Bearer nope', 'rt-1'):
            self.assertNotIn(leaked, joined)
        self.assertIn('https://example.test/hook', joined)

    def test_a_long_value_is_described_rather_than_quoted(self):
        detail = describe_call('mcp__7__send_email_ab12cd34', {
            'to': 'a@b.test', 'body': 'word ' * 400,
        })

        body = dict(zip(_labels(detail), _values(detail)))['Body']
        self.assertRegex(body, r'^\d+ words$')
        self.assertLess(len(body), 20)

    def test_newlines_never_reach_a_field(self):
        """A value is placed in a layout and, in the Inbox, near a markdown
        renderer. A newline breaks out of a single-line field; `#` at the head
        of one would style the screen asking to be approved."""
        detail = describe_call('write_file', {
            'path': '/Chat/x.md', 'content': '# Big\n\nand more',
        })
        for value in _values(detail):
            self.assertNotIn('\n', value)

    def test_the_identifying_arguments_come_first(self):
        detail = describe_call('mcp__7__send_email_ab12cd34', {
            'importance': 'high', 'format': 'html', 'draft': False,
            'reply_to': 'x@y.test', 'signature': 'yes', 'template': 't1',
            'to': 'priya@acme.test', 'subject': 'Q3',
        })

        self.assertLessEqual(len(detail['fields']), MAX_FIELDS)
        self.assertEqual(_labels(detail)[:2], ['To', 'Subject'])

    def test_empty_arguments_are_dropped(self):
        """An unset optional parameter is noise on an approval screen, and MCP
        schemas carry a dozen of them."""
        detail = describe_call('mcp__7__send_email_ab12cd34', {
            'to': 'a@b.test', 'cc': None, 'bcc': [], 'headers': {}, 'note': '',
        })
        self.assertEqual(_labels(detail), ['To'])

    def test_a_nested_object_is_named_by_its_keys(self):
        detail = describe_call('mcp__7__create_ab12cd34', {
            'properties': {'Name': 'x', 'Status': 'y', 'Owner': 'z'},
        })
        self.assertEqual(_values(detail), ['{Name, Status, Owner}'])

    def test_arguments_of_any_shape_are_accepted(self):
        for args in (None, {}, {'x': object()}, {'n': 3}, {'ok': True},
                     {'items': list(range(50))}):
            with self.subTest(args=args):
                describe_call('write_file', args)

    def test_a_value_is_bounded_even_when_it_is_not_long_enough_to_summarise(self):
        detail = describe_call('write_file', {'path': 'p' * (LONG_VALUE_CHARS - 1)})
        self.assertLess(len(_values(detail)[0]), LONG_VALUE_CHARS)


class ServerLookupTests(TestCase):
    """The async wrapper is the only half allowed to touch the database."""

    def test_it_names_the_connection(self):
        from mcp_integration.models import MCPServer

        server = MCPServer.objects.create(name='Gmail', user=None)
        detail = async_to_sync(describe_call_async)(
            f'mcp__{server.id}__send_email_ab12cd34', {'to': 'a@b.test'},
        )

        self.assertEqual(detail['server'], 'Gmail')
        self.assertEqual(detail['title'], 'Send email · Gmail')

    def test_a_missing_connection_is_not_an_error(self):
        """A row can be deleted between the pause and the read."""
        detail = async_to_sync(describe_call_async)(
            'mcp__999999__send_email_ab12cd34', {},
        )
        self.assertEqual(detail['server'], '')
        self.assertEqual(detail['tool'], 'send_email')

    def test_a_builtin_needs_no_lookup(self):
        detail = async_to_sync(describe_call_async)('write_file', {'path': '/a'})
        self.assertEqual(detail['server'], '')
        self.assertEqual(detail['title'], 'Save a file')
