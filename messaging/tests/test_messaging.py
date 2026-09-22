"""
Talk (P1): five tools, one outbox, guarded sends, verified webhooks.

Pinned: drafts never leave the platform; unattended sends need recipients;
a run cannot spam (20 total, 5 per recipient); costs land in the ledger for
WhatsApp/SMS only; every webhook refusal is the same 404; the inbox is a
window (retention purge).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import timedelta
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from chat.tools import execute_tool
from logs.models import CostEntry
from messaging.models import InboundMessage, MessagingAccount, OutboundMessage
from messaging.retention import purge_expired

User = get_user_model()


class TalkToolTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('talker', 't@example.com', 'pw')
        self.account = MessagingAccount.objects.create(
            user=self.user, channel='slack', label='team', verified=True)
        self.ctx = {'user_id': self.user.id, 'caller': 'chat'}

    def call(self, name, args, **extra):
        ctx = dict(self.ctx)
        ctx.update(extra)
        return json.loads(async_to_sync(execute_tool)(name, args, ctx))

    def test_a_draft_never_leaves_the_platform(self):
        out = self.call('message_draft', {
            'channel': 'slack', 'to': '#team-ops', 'body': 'Deploy at 5?'})
        self.assertIn('draft_id', out)
        row = OutboundMessage.objects.get(id=out['draft_id'])
        self.assertEqual(row.status, 'draft')
        self.assertEqual(row.provider_message_id, '')

    def test_a_draft_sends_through_the_provider(self):
        draft = OutboundMessage.objects.create(
            user=self.user, account=self.account, channel='slack',
            to='#team-ops', body='Deploy at 5?', status='draft')
        with mock.patch('chat.tools.messaging.slack.send', autospec=True) as send:
            async def _s(*a, **k):
                return 'ts-1'
            send.side_effect = _s
            out = self.call('message_send', {
                'channel': 'slack', 'to': '#team-ops', 'draft_id': draft.id})
        self.assertTrue(out['sent'])
        draft.refresh_from_db()
        self.assertEqual((draft.status, draft.provider_message_id), ('sent', 'ts-1'))

    def test_a_send_without_a_connected_channel_says_how(self):
        out = self.call('message_send', {
            'channel': 'slack', 'to': '#team-ops', 'body': 'hi'})
        self.assertIn('not connected', out['error'])

    def test_teams_names_its_blocker(self):
        out = self.call('message_send', {
            'channel': 'teams', 'to': 'chat', 'body': 'hi'})
        self.assertIn('admin', out['error'].lower())

    def test_unattended_sends_need_recipients(self):
        out = self.call('message_send', {
            'channel': 'slack', 'to': '#team-ops', 'body': 'hi'},
            caller='trigger')
        self.assertIn('allowlist', out['error'])
        out = self.call('message_send', {
            'channel': 'slack', 'to': '#strangers', 'body': 'hi'},
            caller='trigger', recipients=['#team-ops'])
        self.assertIn('allowlist', out['error'])

    def test_an_allowlisted_unattended_send_goes(self):
        with mock.patch('chat.tools.messaging.slack.send', autospec=True) as send:
            async def _s(*a, **k):
                return 'ts-2'
            send.side_effect = _s
            out = self.call('message_send', {
                'channel': 'slack', 'to': '#team-ops', 'body': 'hi'},
                caller='trigger', recipients=['#team-ops'])
        self.assertTrue(out['sent'])

    def test_a_run_cannot_spam_one_recipient(self):
        ctx = dict(self.ctx)
        with mock.patch('chat.tools.messaging.slack.send', autospec=True) as send:
            async def _s(*a, **k):
                return 'ts'
            send.side_effect = _s
            for _ in range(5):
                out = json.loads(async_to_sync(execute_tool)(
                    'message_send',
                    {'channel': 'slack', 'to': '#team-ops', 'body': 'hi'}, ctx))
                self.assertTrue(out['sent'])
            out = json.loads(async_to_sync(execute_tool)(
                'message_send',
                {'channel': 'slack', 'to': '#team-ops', 'body': 'hi'}, ctx))
        self.assertIn('already got 5', out['error'])

    def test_slack_sends_cost_nothing_whatsapp_costs(self):
        with mock.patch('chat.tools.messaging.whatsapp.send', autospec=True) as send:
            async def _s(*a, **k):
                return 'wamid-1'
            send.side_effect = _s
            ctx = dict(self.ctx)
            out = json.loads(async_to_sync(execute_tool)(
                'message_send',
                {'channel': 'whatsapp', 'to': '+911234567890', 'body': 'hi'}, ctx))
        self.assertTrue(out['sent'])
        row = CostEntry.objects.get(user=self.user, kind='whatsapp')
        self.assertEqual(row.amount_inr, 1)
        self.assertTrue(row.estimated)

    def test_search_reads_our_own_rows(self):
        InboundMessage.objects.create(
            user=self.user, channel='whatsapp', sender='+91111', body='price?')
        out = self.call('message_search', {'channel': 'whatsapp', 'query': 'price'})
        self.assertEqual(len(out['messages']), 1)
        out = self.call('message_read', {'channel': 'whatsapp', 'conversation': '+91111'})
        self.assertEqual(out['messages'][0]['sender'], '+91111')

    def test_send_is_sensitive_and_irreversible(self):
        from chat.tools.registry import get

        tool = get('message_send')
        self.assertTrue(tool.sensitive)
        self.assertEqual(tool.effect, 'irreversible')
        self.assertEqual(get('message_draft').effect, 'reversible')


class WebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('hooker', 'h@example.com', 'pw')
        self.account = MessagingAccount.objects.create(
            user=self.user, channel='slack', label='team', verified=True)

    def url(self, channel='slack', secret=None):
        return reverse('messaging:message_hook',
                       args=[channel, secret or self.account.secret])

    @override_settings(SLACK_SIGNING_SECRET='shh')
    def test_a_signed_slack_event_is_stored_and_pings(self):
        from notifications.models import Notification

        body = json.dumps({'event': {'type': 'message', 'user': 'U1',
                                     'text': 'the deploy broke', 'ts': '1'}}).encode()
        ts = str(int(time.time()))
        sig = 'v0=' + hmac.new(b'shh', f'v0:{ts}:'.encode() + body,
                               hashlib.sha256).hexdigest()
        response = self.client.post(
            self.url(), data=body, content_type='application/json',
            HTTP_X_SLACK_REQUEST_TIMESTAMP=ts, HTTP_X_SLACK_SIGNATURE=sig)
        self.assertEqual(response.status_code, 200)
        row = InboundMessage.objects.get(user=self.user, channel='slack')
        self.assertEqual(row.sender, 'U1')
        self.assertTrue(Notification.objects.filter(user=self.user).exists())

    def test_an_unsigned_event_is_a_404(self):
        response = self.client.post(
            self.url(), data=json.dumps({'event': {'type': 'message'}}),
            content_type='application/json')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {'detail': 'Not found.'})

    def test_a_wrong_secret_is_the_same_404(self):
        response = self.client.post(
            self.url(secret='nope'), data=b'{}', content_type='application/json')
        self.assertEqual(response.status_code, 404)

    @override_settings(WHATSAPP_VERIFY_TOKEN='tok')
    def test_whatsapp_handshake_is_answered(self):
        wa = MessagingAccount.objects.create(
            user=self.user, channel='whatsapp', label='biz')
        response = self.client.get(
            reverse('messaging:message_hook', args=['whatsapp', wa.secret]),
            {'hub.mode': 'subscribe', 'hub.verify_token': 'tok',
             'hub.challenge': 'CHAL'})
        self.assertEqual(response.content, b'CHAL')

    def test_a_whatsapp_message_is_parsed(self):
        wa = MessagingAccount.objects.create(
            user=self.user, channel='whatsapp', label='biz')
        body = {'entry': [{'changes': [{'value': {
            'messages': [{'from': '+91111', 'text': {'body': 'price?'}}]}}]}]}
        response = self.client.post(
            reverse('messaging:message_hook', args=['whatsapp', wa.secret]),
            data=json.dumps(body), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(InboundMessage.objects.filter(
            user=self.user, sender='+91111').exists())

class RetentionTests(TestCase):
    def test_old_inbound_is_purged_outbound_stays(self):
        user = User.objects.create_user('old', 'o@example.com', 'pw')
        old = InboundMessage.objects.create(user=user, channel='sms', sender='x')
        InboundMessage.objects.filter(id=old.id).update(
            received_at=timezone.now() - timedelta(days=91))
        OutboundMessage.objects.create(user=user, channel='sms', to='y', body='z')
        self.assertEqual(purge_expired(), 1)
        self.assertEqual(OutboundMessage.objects.count(), 1)


class SlackTokenTests(TestCase):
    """The adapter posts with the bot token and searches with the user
    token. The vault grew those names at different times, so both the
    current and the legacy field names must resolve."""

    def setUp(self):
        self.user = User.objects.create_user('slacktok', 's@example.com', 'pw')

    def _tokens(self, data):
        from chat.tools.messaging import slack

        credential = mock.Mock()
        credential.get_credential_data.return_value = data
        with mock.patch(
                'credentials.manager.CredentialManager.lookup_by_slug_sync',
                return_value=credential):
            return async_to_sync(slack._tokens)(self.user.id, None)

    def test_bot_and_user_tokens_come_from_their_fields(self):
        self.assertEqual(
            self._tokens({'token': 'xoxb-a', 'teamId': 'T1',
                          'user_token': 'xoxp-b'}),
            ('xoxb-a', 'xoxp-b'))

    def test_the_legacy_bot_token_name_still_works(self):
        self.assertEqual(
            self._tokens({'bot_token': 'xoxb-old'}), ('xoxb-old', None))

    def test_no_credential_is_no_tokens(self):
        with mock.patch(
                'credentials.manager.CredentialManager.lookup_by_slug_sync',
                return_value=None):
            from chat.tools.messaging import slack

            self.assertEqual(
                async_to_sync(slack._tokens)(self.user.id, None), (None, None))


class FakeBotResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {'ok': True}

    def json(self):
        return self._payload


class TelegramTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('gram', 'g@example.com', 'pw')
        self.account = MessagingAccount.objects.create(
            user=self.user, channel='telegram', label='shop', verified=True)
        self.ctx = {'user_id': self.user.id, 'caller': 'chat'}

    def call(self, name, args, **extra):
        ctx = dict(self.ctx)
        ctx.update(extra)
        return json.loads(async_to_sync(execute_tool)(name, args, ctx))

    def _vault(self, data):
        credential = mock.Mock()
        credential.get_credential_data.return_value = data
        return mock.patch(
            'credentials.manager.CredentialManager.lookup_by_slug_sync',
            return_value=credential)

    def _http(self, response):
        client = mock.Mock()

        async def _post(*args, **kwargs):
            return response
        client.post = _post
        return mock.patch(
            'workflow_backend.httpclient.shared_client', return_value=client)

    def test_send_posts_to_the_bot_api(self):
        with self._vault({'token': 'tok'}), self._http(
                FakeBotResponse(payload={'ok': True, 'result': {'message_id': 42}})):
            out = self.call('message_send', {
                'channel': 'telegram', 'to': '123', 'body': 'hi'})
        self.assertTrue(out['sent'])
        self.assertEqual(out['provider_message_id'], '42')
        row = OutboundMessage.objects.get(user=self.user, channel='telegram')
        self.assertEqual((row.status, row.to), ('sent', '123'))

    def test_the_legacy_token_name_is_accepted(self):
        with self._vault({'bot_token': 'tok'}), self._http(FakeBotResponse()):
            out = self.call('message_send', {
                'channel': 'telegram', 'to': '123', 'body': 'hi'})
        self.assertTrue(out['sent'])

    def test_send_without_a_token_says_how(self):
        with mock.patch(
                'credentials.manager.CredentialManager.lookup_by_slug_sync',
                return_value=None):
            out = self.call('message_send', {
                'channel': 'telegram', 'to': '123', 'body': 'hi'})
        self.assertIn('not connected', out['error'])

    def test_a_bad_token_is_reported_not_retried(self):
        with self._vault({'token': 'bad'}), self._http(FakeBotResponse(401)):
            out = self.call('message_send', {
                'channel': 'telegram', 'to': '123', 'body': 'hi'})
        self.assertIn('token', out['error'])

    def test_telegram_sends_cost_nothing(self):
        with self._vault({'token': 'tok'}), self._http(FakeBotResponse()):
            out = self.call('message_send', {
                'channel': 'telegram', 'to': '123', 'body': 'hi'})
        self.assertTrue(out['sent'])
        self.assertFalse(CostEntry.objects.filter(user=self.user).exists())

    def test_search_and_targets_read_our_own_rows(self):
        InboundMessage.objects.create(
            user=self.user, account=self.account, channel='telegram',
            sender='123', body='do you ship?')
        out = self.call('message_search', {'channel': 'telegram', 'query': 'ship'})
        self.assertEqual(len(out['messages']), 1)
        out = self.call('message_read', {'channel': 'telegram', 'conversation': '123'})
        self.assertEqual(out['messages'][0]['sender'], '123')
        out = self.call('message_channels', {'channel': 'telegram'})
        self.assertEqual(out['channels'][0]['targets'],
                         [{'id': '123', 'name': '123', 'kind': 'chat'}])

    def test_a_draft_never_leaves_the_platform(self):
        out = self.call('message_draft', {
            'channel': 'telegram', 'to': '123', 'body': 'yes, tuesday'})
        row = OutboundMessage.objects.get(id=out['draft_id'])
        self.assertEqual(row.status, 'draft')


class TelegramWebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('gramhook', 'gh@example.com', 'pw')
        self.account = MessagingAccount.objects.create(
            user=self.user, channel='telegram', label='shop', verified=True)

    def url(self, secret=None):
        return reverse('messaging:message_hook',
                       args=['telegram', secret or self.account.secret])

    def headers(self, secret=None):
        return {'HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN': secret or self.account.secret}

    def update(self, chat_id='123', username='ann', text='hello?'):
        return {'update_id': 7, 'message': {
            'message_id': 1, 'chat': {'id': int(chat_id), 'type': 'private'},
            'from': {'username': username}, 'text': text}}

    def test_a_signed_update_is_stored_and_pings(self):
        from notifications.models import Notification

        response = self.client.post(
            self.url(), data=json.dumps(self.update()),
            content_type='application/json', **self.headers())
        self.assertEqual(response.status_code, 200)
        row = InboundMessage.objects.get(user=self.user, channel='telegram')
        # The chat id is the addressable sender; the name is for display.
        self.assertEqual(row.sender, '123')
        self.assertEqual(row.body, 'hello?')
        self.assertTrue(Notification.objects.filter(user=self.user).exists())

    def test_a_wrong_secret_is_the_same_404(self):
        response = self.client.post(
            self.url(), data=json.dumps(self.update()),
            content_type='application/json', **self.headers(secret='nope'))
        self.assertEqual(response.status_code, 404)
        self.assertFalse(InboundMessage.objects.exists())

    def test_a_missing_secret_is_the_same_404(self):
        response = self.client.post(
            self.url(), data=json.dumps(self.update()),
            content_type='application/json')
        self.assertEqual(response.status_code, 404)
        self.assertFalse(InboundMessage.objects.exists())

    def test_a_non_message_update_is_acknowledged_not_stored(self):
        response = self.client.post(
            self.url(), data=json.dumps({'update_id': 8}),
            content_type='application/json', **self.headers())
        self.assertEqual(response.json(), {'ok': True})
        self.assertFalse(InboundMessage.objects.exists())
