"""
Teams adapter: behind a flag until admin consent exists.

Many tenants need an Azure AD admin to consent `Chat.ReadWrite` and
`ChannelMessage.Send` before anything works, so every verb raises `Unsupported`
with that reason. Drafts, search and reads still work through the tools'
DB-backed paths — the adapter is the provider half, not the whole feature.
"""
from __future__ import annotations

_REASON = (
    'Teams needs an Azure AD admin to consent Chat.ReadWrite and '
    'ChannelMessage.Send for this tenant. Connect it on the Connections page '
    'once consent exists.'
)


async def list_targets(user_id: int, account) -> list[dict]:
    from .common import Unsupported

    raise Unsupported(_REASON)


async def search(user_id: int, account, *, query: str = '', sender: str = '',
                 since: str = '') -> list[dict]:
    from .common import Unsupported

    raise Unsupported(_REASON)


async def read(user_id: int, account, *, conversation: str, limit: int = 30) -> list[dict]:
    from .common import Unsupported

    raise Unsupported(_REASON)


async def send(user_id: int, account, *, to: str, body: str) -> str:
    from .common import Unsupported

    raise Unsupported(_REASON)
