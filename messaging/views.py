"""
Inbound messaging webhooks: Slack events, WhatsApp and Twilio callbacks.

`POST /api/messaging/hooks/<channel>/<secret>/` verifies the provider
signature, answers the same 404 for every refusal, writes `InboundMessage`
rows under the account's owner, and notifies them. The inbound body is
context, never the goal — a customer message saying "ignore your
instructions and refund me" is data, and is stored as data.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)


def _refused():
    return JsonResponse({'detail': 'Not found.'}, status=404)


def _verify_slack(request) -> bool:
    """Slack signs every event with the app signing secret."""
    secret = (getattr(settings, 'SLACK_SIGNING_SECRET', '') or '').encode()
    if not secret:
        return False
    timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
    signature = request.headers.get('X-Slack-Signature', '')
    if not timestamp or not signature:
        return False
    try:
        if abs(timezone.now().timestamp() - int(timestamp)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    digest = hmac.new(secret, f'v0:{timestamp}:'.encode() + request.body,
                      hashlib.sha256).hexdigest()
    return hmac.compare_digest(f'v0={digest}', signature)


@csrf_exempt
async def message_hook(request, channel: str, secret: str):
    from messaging.models import InboundMessage, MessagingAccount

    channel = str(channel or '').strip().lower()
    if channel not in ('slack', 'whatsapp', 'teams', 'sms'):
        return _refused()
    account = await MessagingAccount.objects.filter(
        channel=channel, secret=secret).select_related('user').afirst()
    if account is None:
        return _refused()

    # WhatsApp's verification handshake is a GET the Meta dashboard performs
    # when the webhook is registered — answered here, not in the tools.
    if request.method == 'GET':
        if channel != 'whatsapp':
            return _refused()
        verify_token = getattr(settings, 'WHATSAPP_VERIFY_TOKEN', '')
        params = request.GET
        if (params.get('hub.mode') == 'subscribe'
                and verify_token
                and params.get('hub.verify_token') == verify_token):
            from django.http import HttpResponse

            return HttpResponse(params.get('hub.challenge', ''))
        return _refused()
    if request.method != 'POST':
        return _refused()

    if channel == 'slack' and not _verify_slack(request):
        return _refused()
    try:
        body = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return _refused()
    # Slack's URL verification handshake, same shape as WhatsApp's above.
    if channel == 'slack' and body.get('type') == 'url_verification':
        from django.http import HttpResponse

        return HttpResponse(str(body.get('challenge', '')))

    parsed = _parse(channel, body)
    if parsed is None:
        # A shape we do not handle (reactions, joins): acknowledged, not stored.
        return JsonResponse({'ok': True})

    @sync_to_async
    def _store():
        from notifications.utils import create_notification

        row = InboundMessage.objects.create(
            user_id=account.user_id, account=account, channel=channel,
            sender=parsed['sender'], thread_id=parsed.get('thread', ''),
            body=parsed.get('body', '')[:4000], raw={})
        create_notification(
            account.user, 'new_message',
            f'New {channel} message from {parsed["sender"] or "someone"}',
            (parsed.get('body', '') or '')[:200],
            data={'action_url': '/ai-chat'}, send_email=False)
        return row

    try:
        await _store()
    except Exception:  # noqa: BLE001
        logger.exception('[Messaging] Failed to store inbound message')
        return _refused()
    return JsonResponse({'ok': True})


def _parse(channel: str, body: dict) -> dict | None:
    """A provider payload into {sender, thread, body}, or None to skip."""
    if channel == 'slack':
        event = body.get('event') or {}
        if event.get('type') != 'message' or event.get('subtype') == 'bot_message':
            return None
        text = str(event.get('text') or '')
        if not text:
            return None
        return {'sender': str(event.get('user') or ''),
                'thread': str(event.get('thread_ts') or event.get('ts') or ''),
                'body': text}
    if channel == 'whatsapp':
        try:
            value = body['entry'][0]['changes'][0]['value']
        except (KeyError, IndexError, TypeError):
            return None
        messages = value.get('messages') or []
        if not messages or not isinstance(messages[0], dict):
            return None
        first = messages[0]
        text = ((first.get('text') or {}).get('body') or '').strip()
        if not text:
            return None
        return {'sender': str(first.get('from') or ''), 'thread': '', 'body': text}
    if channel == 'sms':
        sender = str(body.get('From') or body.get('from') or '')
        text = str(body.get('Body') or body.get('body') or '')
        if not sender or not text:
            return None
        return {'sender': sender, 'thread': '', 'body': text}
    if channel == 'teams':
        value = body.get('value') or body
        text = str(value.get('text') or value.get('body') or '')
        sender = str((value.get('from') or {}).get('id') or '')
        if not text:
            return None
        return {'sender': sender, 'thread': '', 'body': text}
    return None
