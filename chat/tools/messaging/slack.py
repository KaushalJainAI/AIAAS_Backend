"""
Slack adapter: the one channel fully reachable in v1.

Posting uses the bot token, search uses the user token (`search:read`) — two
different authorities, and the tools say which is missing. Posting as bot vs
as user is a per-account choice in `config.post_as`.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_API = 'https://api.slack.com/api'


async def _tokens(user_id: int, account) -> tuple[str | None, str | None]:
    from .common import vault_field

    slug = (account.credential_slug if account else '') or 'slack'
    data = None
    try:
        from asgiref.sync import sync_to_async

        from credentials.manager import CredentialManager

        credential = await sync_to_async(CredentialManager.lookup_by_slug_sync)(
            slug, user_id)
        if credential is not None:
            try:
                data = credential.get_credential_data() or {}
            except Exception:  # noqa: BLE001
                data = None
    except Exception:  # noqa: BLE001
        data = None
    if not data:
        return None, None
    # The vault's `token` field is the bot token (xoxb); `bot_token` is the
    # same value under the older standalone seeder's name. Either unlocks
    # posting; only `user_token` (xoxp) unlocks search.
    return data.get('token') or data.get('bot_token'), data.get('user_token')


def _need_what(bot: str | None, user: str | None, verb: str) -> None:
    from .common import Unsupported

    if verb in ('send', 'targets') and not bot:
        raise Unsupported(
            'Slack is not connected: add a bot token on the Connections page.')
    if verb in ('search', 'read') and not (user or bot):
        raise Unsupported(
            'Slack is not connected: add a token on the Connections page.')


async def list_targets(user_id: int, account) -> list[dict]:
    """Channels and DMs the account can address."""
    from workflow_backend.httpclient import shared_client

    from .common import Unsupported

    bot, _user = await _tokens(user_id, account)
    _need_what(bot, _user, 'targets')
    try:
        resp = await shared_client().get(
            f'{_API}/conversations.list',
            headers={'Authorization': f'Bearer {bot}'},
            params={'types': 'public_channel,private_channel,im', 'limit': 200},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise Unsupported('Slack could not be reached. Try again shortly.') from exc
    if resp.status_code == 429:
        raise Unsupported('Slack is rate-limited. Try again later.')
    try:
        payload = resp.json()
    except ValueError as exc:
        raise Unsupported('Slack returned something unreadable.') from exc
    if not payload.get('ok'):
        raise Unsupported(f"Slack refused: {payload.get('error', 'unknown error')}.")
    return [
        {'id': c.get('id'), 'name': c.get('name') or c.get('user'),
         'kind': 'dm' if c.get('is_im') else 'channel'}
        for c in payload.get('channels', []) if isinstance(c, dict)
    ]


async def search(user_id: int, account, *, query: str = '', sender: str = '',
                 since: str = '') -> list[dict]:
    """Search messages. Needs the user token (`search:read`)."""
    from workflow_backend.httpclient import shared_client

    from .common import Unsupported

    bot, user = await _tokens(user_id, account)
    token = user or bot
    if not token:
        raise Unsupported('Slack is not connected: add a token on the Connections page.')
    params = {'query': query or '', 'count': 50}
    if sender:
        params['query'] = f'from:<@{sender}> {params["query"]}'.strip()
    try:
        resp = await shared_client().get(
            f'{_API}/search.messages',
            headers={'Authorization': f'Bearer {token}'},
            params=params, timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise Unsupported('Slack could not be reached. Try again shortly.') from exc
    if resp.status_code >= 400:
        raise Unsupported(f'Slack refused the search ({resp.status_code}).')
    try:
        payload = resp.json()
    except ValueError as exc:
        raise Unsupported('Slack returned something unreadable.') from exc
    out = []
    for m in (payload.get('messages') or {}).get('matches', []):
        if isinstance(m, dict):
            out.append({'id': m.get('ts'), 'channel': m.get('channel', {}).get('id'),
                        'sender': m.get('user'), 'text': m.get('text')})
    return out


async def read(user_id: int, account, *, conversation: str, limit: int = 30) -> list[dict]:
    from workflow_backend.httpclient import shared_client

    from .common import Unsupported

    bot, user = await _tokens(user_id, account)
    token = bot or user
    _need_what(token, user, 'read')
    try:
        resp = await shared_client().get(
            f'{_API}/conversations.history',
            headers={'Authorization': f'Bearer {token}'},
            params={'channel': conversation, 'limit': max(1, min(limit, 100))},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise Unsupported('Slack could not be reached. Try again shortly.') from exc
    if resp.status_code >= 400:
        raise Unsupported(f'Slack refused the read ({resp.status_code}).')
    try:
        payload = resp.json()
    except ValueError as exc:
        raise Unsupported('Slack returned something unreadable.') from exc
    return [
        {'id': m.get('ts'), 'sender': m.get('user'), 'text': m.get('text')}
        for m in payload.get('messages', []) if isinstance(m, dict)
    ]


async def send(user_id: int, account, *, to: str, body: str) -> str:
    """Post to a channel or DM. Returns the provider message id."""
    from workflow_backend.httpclient import shared_client

    from .common import Unsupported

    bot, _user = await _tokens(user_id, account)
    _need_what(bot, _user, 'send')
    post_as = ((account.config or {}).get('post_as', 'bot')
               if account else 'bot')
    try:
        resp = await shared_client().post(
            f'{_API}/chat.postMessage',
            headers={'Authorization': f'Bearer {bot}'},
            json={'channel': to, 'text': body,
                  **({'username': post_as} if post_as != 'bot' else {})},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        raise Unsupported('Slack could not be reached. Try again shortly.') from exc
    if resp.status_code == 429:
        raise Unsupported('Slack is rate-limited. Try again later.')
    try:
        payload = resp.json()
    except ValueError as exc:
        raise Unsupported('Slack returned something unreadable.') from exc
    if not payload.get('ok'):
        raise Unsupported(f"Slack refused the send: {payload.get('error', 'unknown error')}.")
    return str(payload.get('ts') or '')
