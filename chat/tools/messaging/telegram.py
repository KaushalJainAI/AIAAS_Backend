"""
Telegram adapter over the Bot API.

No business verification, no templates, no per-message cost — a bot token
from @BotFather is the whole setup. Sends go to `sendMessage` with the chat
id; history lives in our own inbox (Telegram keeps per-bot history, but
reading it back through the API is a second mechanism for what the webhook
already stores). Until the token is stored and the webhook registered (see
`register_telegram_webhook`), every verb but the DB-backed ones raises
`Unsupported` with the setup reason.

Two seeders have named the token field differently over time
(`seed_connector_credentials` says `token`, the standalone
`populate_credentials.py` says `bot_token`), so both are accepted.
"""
from __future__ import annotations


def _creds_error():
    from .common import Unsupported

    raise Unsupported(
        'Telegram is not connected: talk to @BotFather, create a bot, and '
        'store its token on the Connections page.')


async def _token(user_id: int, account) -> str:
    from .common import vault_field

    slug = (account.credential_slug if account else '') or 'telegram'
    token = await vault_field(user_id, slug, 'token')
    if token is None:
        token = await vault_field(user_id, slug, 'bot_token')
    if not token:
        _creds_error()
    assert token is not None
    return token


async def list_targets(user_id: int, account) -> list[dict]:
    """Chats that have written to the bot — the only addressable set, since a
    bot cannot open a conversation first."""
    from asgiref.sync import sync_to_async

    from messaging.models import InboundMessage

    senders = await sync_to_async(list)(
        InboundMessage.objects.filter(user_id=user_id, channel='telegram')
        .exclude(sender='').order_by().values_list('sender', flat=True).distinct()[:200]
    )
    return [{'id': s, 'name': s, 'kind': 'chat'} for s in senders]


async def search(user_id: int, account, *, query: str = '', sender: str = '',
                 since: str = '') -> list[dict]:
    """Telegram search covers only messages received through our webhook."""
    from asgiref.sync import sync_to_async

    from messaging.models import InboundMessage, OutboundMessage

    rows = await sync_to_async(list)(
        InboundMessage.objects.filter(user_id=user_id, channel='telegram')
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
        OutboundMessage.objects.filter(user_id=user_id, channel='telegram')
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
    """Post to a chat id. Returns the provider message id."""
    from workflow_backend.httpclient import shared_client

    from .common import Unsupported

    token = await _token(user_id, account)
    try:
        resp = await shared_client().post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': to, 'text': body}, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise Unsupported('Telegram could not be reached. Try again shortly.') from exc
    if resp.status_code == 429:
        raise Unsupported('Telegram is rate-limited. Try again later.')
    if resp.status_code == 401:
        raise Unsupported(
            'Telegram refused the token. Check the bot token on the '
            'Connections page.')
    if resp.status_code >= 400:
        raise Unsupported(f'Telegram refused the send ({resp.status_code}).')
    try:
        payload = resp.json()
    except ValueError as exc:
        raise Unsupported('Telegram returned something unreadable.') from exc
    if not payload.get('ok'):
        raise Unsupported(
            f"Telegram refused the send: {payload.get('description', 'unknown error')}.")
    result = payload.get('result') or {}
    return str(result.get('message_id') or '')


def register_webhook(user_id: int, account, base_url: str) -> str:
    """Point the bot at this platform's webhook. Returns the webhook URL.

    Synchronous: used by the management command and the account view, neither
    of which runs in the turn loop. Registers the account's own path secret
    as Telegram's `secret_token`, which `message_hook` verifies.
    """
    import httpx

    from .common import Unsupported

    token = None
    try:
        from credentials.manager import CredentialManager

        slug = (account.credential_slug if account else '') or 'telegram'
        credential = CredentialManager.lookup_by_slug_sync(slug, user_id)
        data = (credential.get_credential_data() or {}) if credential else {}
        token = data.get('token') or data.get('bot_token')
    except Exception:  # noqa: BLE001
        token = None
    if not token:
        raise Unsupported(
            'Telegram is not connected: talk to @BotFather, create a bot, and '
            'store its token on the Connections page.')
    if not base_url:
        raise Unsupported('PUBLIC_URL is not set; cannot build the webhook URL.')
    from django.urls import reverse

    url = f"{base_url.rstrip('/')}{reverse('messaging:message_hook', args=['telegram', account.secret])}"
    try:
        resp = httpx.post(
            f'https://api.telegram.org/bot{token}/setWebhook',
            json={'url': url, 'secret_token': account.secret,
                  'allowed_updates': ['message', 'edited_message']},
            timeout=20,
        )
        payload = resp.json()
    except Exception as exc:
        raise Unsupported('Telegram could not be reached. Try again shortly.') from exc
    if not payload.get('ok'):
        raise Unsupported(
            f"Telegram refused: {payload.get('description', 'unknown error')}.")
    account.verified = True
    account.save(update_fields=['verified', 'updated_at'])
    return url
