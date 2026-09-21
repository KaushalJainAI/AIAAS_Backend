"""
SMS adapter over MSG91 (India) or Twilio (elsewhere).

India needs DLT registration — a sender id and template ids — so free text
without them is refused rather than silently undelivered. There is no search
worth offering: SMS has no history API, and the tool says so by searching our
own outbox and inbox instead.
"""
from __future__ import annotations

from django.conf import settings


def _engine() -> str:
    return (getattr(settings, 'SMS_ENGINE', 'none') or 'none').strip().lower()


async def _send_msg91(*, sender: str, to: str, body: str, config: dict) -> str:
    from workflow_backend.httpclient import shared_client

    from .common import Unsupported, vault_field

    key = await vault_field(config.get('user_id'), 'sms', 'api_key')
    if not key:
        raise Unsupported('SMS is not connected: add an MSG91 key on the Connections page.')
    template = (config.get('template_id') or '').strip()
    if not template:
        raise Unsupported(
            'India needs DLT registration: set a sender id and template id on '
            'the SMS connection, or the message is undelivered.')
    try:
        resp = await shared_client().post(
            'https://control.msg91.com/api/v5/flow/',
            headers={'authkey': key},
            json={'sender': sender or 'TXTSMS', 'mobiles': to,
                  'template_id': template, 'message': body},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise Unsupported('The SMS provider could not be reached.') from exc
    if resp.status_code >= 400:
        raise Unsupported(f'The SMS provider refused the send ({resp.status_code}).')
    return 'msg91'


async def send(user_id: int, account, *, to: str, body: str) -> str:
    from .common import Unsupported

    if _engine() == 'none':
        raise Unsupported(
            'SMS is not connected (SMS_ENGINE=none): add MSG91 for India or '
            'Twilio elsewhere, with DLT sender and template ids for India.')
    if _engine() == 'twilio':
        raise Unsupported(
            'Twilio is not wired yet; MSG91 is the v1 route. Say so rather '
            'than retrying.')
    config = dict((account.config or {}) if account else {})
    config['user_id'] = user_id
    sender = str(config.get('sender_id') or '')
    return await _send_msg91(sender=sender, to=to, body=body, config=config)


async def list_targets(user_id: int, account) -> list[dict]:
    """Numbers this user has texted — the only addressable set we keep."""
    from asgiref.sync import sync_to_async

    from messaging.models import OutboundMessage

    numbers = await sync_to_async(list)(
        OutboundMessage.objects.filter(user_id=user_id, channel='sms')
        .exclude(to='').order_by().values_list('to', flat=True).distinct()[:200]
    )
    return [{'id': n, 'name': n, 'kind': 'number'} for n in numbers]


async def search(user_id: int, account, *, query: str = '', sender: str = '',
                 since: str = '') -> list[dict]:
    from asgiref.sync import sync_to_async

    from messaging.models import InboundMessage, OutboundMessage

    out = []
    for row in await sync_to_async(list)(
            InboundMessage.objects.filter(user_id=user_id, channel='sms')
            .order_by('-received_at')[:200]):
        if query and query.lower() not in (row.body or '').lower():
            continue
        if sender and row.sender != sender:
            continue
        out.append({'id': f'in-{row.id}', 'sender': row.sender,
                    'text': row.body, 'at': row.received_at.isoformat()})
    for row in await sync_to_async(list)(
            OutboundMessage.objects.filter(user_id=user_id, channel='sms')
            .exclude(status='draft').order_by('-created_at')[:50]):
        if query and query.lower() not in (row.body or '').lower():
            continue
        out.append({'id': f'out-{row.id}', 'to': row.to,
                    'text': row.body, 'at': row.created_at.isoformat()})
    return out[:50]


async def read(user_id: int, account, *, conversation: str, limit: int = 30) -> list[dict]:
    rows = await search(user_id, account, sender=conversation)
    return rows[:max(1, min(limit, 100))]


async def send(user_id: int, account, *, to: str, body: str) -> str:
    from .common import Unsupported

    if _engine() == 'none':
        raise Unsupported(
            'SMS is not connected (SMS_ENGINE=none): add MSG91 for India or '
            'Twilio elsewhere, with DLT sender and template ids for India.')
    if _engine() == 'twilio':
        raise Unsupported(
            'Twilio is not wired yet; MSG91 is the v1 route. Say so rather '
            'than retrying.')
    config = dict((account.config or {}) if account else {})
    config['user_id'] = user_id
    sender = str(config.get('sender_id') or '')
    return await _send_msg91(sender=sender, to=to, body=body, config=config)
