"""
SMS adapter over Twilio.

The vault holds the Twilio triple (`accountSid`, `authToken`, `fromNumber`)
under the seeded `twilio` type; an account may point at another slug holding
the same fields. The sender number may also live on the account config.
Every send is recorded in the cost ledger — SMS spends money per message.
"""
from __future__ import annotations

from django.conf import settings


def _engine() -> str:
    return (getattr(settings, 'SMS_ENGINE', 'none') or 'none').strip().lower()


async def _twilio_config(user_id: int, account) -> tuple[str, str, str]:
    from .common import Unsupported, vault_field

    # Seeded vault shape first; an account may override the slug, and the
    # sender number may live on the account instead of the credential.
    slug = (account.credential_slug if account else '') or 'twilio'
    sid = await vault_field(user_id, slug, 'accountSid')
    token = await vault_field(user_id, slug, 'authToken')
    sender = ((account.config or {}).get('sender_id')
              if account else '') or await vault_field(user_id, slug, 'fromNumber')
    if not sid or not token:
        raise Unsupported('SMS is not connected: add a Twilio account SID and '
                          'auth token on the Connections page.')
    if not sender:
        raise Unsupported('SMS needs a sender number: set the Twilio '
                          'from number on the credential or the account.')
    assert sid is not None and token is not None and sender is not None
    return sid, token, sender


async def send(user_id: int, account, *, to: str, body: str) -> str:
    from workflow_backend.httpclient import shared_client

    from .common import Unsupported

    if _engine() == 'none':
        raise Unsupported(
            'SMS is switched off on this platform (SMS_ENGINE=none). Set it '
            'to twilio and add a Twilio SID and auth token to switch it on.')
    sid, token, sender = await _twilio_config(user_id, account)
    try:
        resp = await shared_client().post(
            f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',
            auth=(sid, token),
            data={'From': sender, 'To': to, 'Body': body},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise Unsupported('The SMS provider could not be reached.') from exc
    if resp.status_code == 429:
        raise Unsupported('The SMS provider is rate-limited. Try again later.')
    if resp.status_code == 401:
        raise Unsupported('Twilio refused the credentials. Check the SID and '
                          'auth token on the Connections page.')
    if resp.status_code >= 400:
        raise Unsupported(f'The SMS provider refused the send ({resp.status_code}).')
    try:
        payload = resp.json()
    except ValueError as exc:
        raise Unsupported('The SMS provider returned something unreadable.') from exc
    if str(payload.get('status') or '') == 'failed':
        raise Unsupported(
            f"Twilio failed the send: {payload.get('error_message', 'unknown error')}.")
    return str(payload.get('sid') or '')


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
