"""
WhatsApp adapter over the Cloud API.

Business-initiated messages outside the 24 h window need a pre-approved
template — `send` picks session message vs template and refuses free text
outside the window, saying why, rather than paying for a message Meta drops.
The window is read from our own inbox: `InboundMessage` rows from that sender
in the last 24 h. Until Business verification completes, every verb but the
DB-backed ones raises `Unsupported` with the setup reason.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone


def _creds_error():
    from .common import Unsupported

    raise Unsupported(
        'WhatsApp is not connected: finish Business verification and add the '
        'phone-number id and token on the Connections page.')


async def _config(user_id: int, account) -> tuple[str, str]:
    from .common import vault_field

    slug = (account.credential_slug if account else '') or 'whatsapp'
    phone_id = await vault_field(user_id, slug, 'phone_number_id')
    token = await vault_field(user_id, slug, 'token')
    if not phone_id or not token:
        _creds_error()
    assert phone_id is not None and token is not None
    return phone_id, token


async def _in_window(user_id: int, sender: str) -> bool:
    from asgiref.sync import sync_to_async

    from messaging.models import InboundMessage

    since = timezone.now() - timedelta(hours=24)
    return await sync_to_async(InboundMessage.objects.filter(
        user_id=user_id, channel='whatsapp', sender=sender,
        received_at__gte=since,
    ).aexists)()


async def list_targets(user_id: int, account) -> list[dict]:
    """Contacts who have messaged the business — the only addressable set
    before verification, and the honest one after it."""
    from asgiref.sync import sync_to_async

    from messaging.models import InboundMessage

    senders = await sync_to_async(list)(
        InboundMessage.objects.filter(user_id=user_id, channel='whatsapp')
        .exclude(sender='').order_by().values_list('sender', flat=True).distinct()[:200]
    )
    return [{'id': s, 'name': s, 'kind': 'contact'} for s in senders]


async def search(user_id: int, account, *, query: str = '', sender: str = '',
                 since: str = '') -> list[dict]:
    """WhatsApp search covers only messages received through our webhook —
    Meta keeps no history API worth using, and the tool says so in the shape
    of what it returns."""
    from asgiref.sync import sync_to_async

    from messaging.models import InboundMessage, OutboundMessage

    rows = await sync_to_async(list)(
        InboundMessage.objects.filter(user_id=user_id, channel='whatsapp')
        .order_by('-received_at')[:200]
    )
    out = []
    for row in rows:
        if query and query.lower() not in (row.body or '').lower():
            continue
        if sender and row.sender != sender:
            continue
        out.append({'id': f'in-{row.id}', 'sender': row.sender,
                    'text': row.body, 'at': row.received_at.isoformat()})
    sent = await sync_to_async(list)(
        OutboundMessage.objects.filter(user_id=user_id, channel='whatsapp')
        .exclude(status='draft').order_by('-created_at')[:50]
    )
    for row in sent:
        if query and query.lower() not in (row.body or '').lower():
            continue
        out.append({'id': f'out-{row.id}', 'to': row.to,
                    'text': row.body, 'at': row.created_at.isoformat()})
    return out[:50]


async def read(user_id: int, account, *, conversation: str, limit: int = 30) -> list[dict]:
    rows = await search(user_id, account, sender=conversation)
    return rows[:max(1, min(limit, 100))]


async def send(user_id: int, account, *, to: str, body: str) -> str:
    """A session message inside 24 h, else a template name in `body` as
    `template:<name>`. Free text outside the window is refused, saying why."""
    from workflow_backend.httpclient import shared_client

    from .common import Unsupported

    phone_id, token = await _config(user_id, account)
    if body.startswith('template:'):
        payload: dict = {'messaging_product': 'whatsapp', 'to': to,
                         'type': 'template',
                         'template': {'name': body[len('template:'):].strip(),
                                      'language': {'code': 'en'}}}
    elif await _in_window(user_id, to):
        payload = {'messaging_product': 'whatsapp', 'to': to,
                   'type': 'text', 'text': {'body': body}}
    else:
        raise Unsupported(
            f'{to} wrote nothing in the last 24 h, so free text would be '
            'dropped by Meta. Send `template:<approved-name>` instead, or wait '
            'for them to write first.')
    try:
        resp = await shared_client().post(
            f'https://graph.facebook.com/v21.0/{phone_id}/messages',
            headers={'Authorization': f'Bearer {token}'},
            json=payload, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise Unsupported('WhatsApp could not be reached. Try again shortly.') from exc
    if resp.status_code == 429:
        raise Unsupported('WhatsApp is rate-limited. Try again later.')
    if resp.status_code >= 400:
        raise Unsupported(f'WhatsApp refused the send ({resp.status_code}).')
    try:
        payload_out = resp.json()
    except ValueError as exc:
        raise Unsupported('WhatsApp returned something unreadable.') from exc
    messages = payload_out.get('messages') or []
    return str((messages[0] or {}).get('id') if messages else '')
