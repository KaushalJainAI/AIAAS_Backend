"""
Shared shapes for the messaging adapters.

An adapter answers four verbs for one channel. Anything it cannot do raises
`Unsupported` with the reason — the tool layer renders that as text, which is
what lets the model choose another channel instead of retrying a dead one.
"""
from __future__ import annotations

from typing import Any


CHANNELS = ('slack', 'whatsapp', 'teams', 'sms')


class Unsupported(RuntimeError):
    """This verb is not available on this channel, and why."""


def validate_channel(channel: str) -> str:
    name = str(channel or '').strip().lower()
    if name not in CHANNELS:
        raise ValueError(f'Channel must be one of {", ".join(CHANNELS)}.')
    return name


def adapter_for(channel: str):
    """The adapter module for `channel`."""
    from . import sms, slack, teams, whatsapp

    return {'slack': slack, 'whatsapp': whatsapp, 'teams': teams, 'sms': sms}[channel]


async def vault_field(user_id: int, slug: str, field: str) -> str | None:
    """One credential field, or None. Never raises — a missing credential is
    "not connected", not a crash."""
    try:
        from asgiref.sync import sync_to_async

        from credentials.manager import CredentialManager

        credential = await sync_to_async(CredentialManager.lookup_by_slug_sync)(
            slug, user_id)
        if credential is None:
            return None
        try:
            data = credential.get_credential_data() or {}
        except Exception:  # noqa: BLE001
            return None
        value = (data or {}).get(field)
        return str(value) if value else None
    except Exception:  # noqa: BLE001
        return None


def cap(items: list[Any], limit: int) -> tuple[list[Any], bool]:
    """First `limit` items and whether there were more. A capped list and a
    complete one must not look alike."""
    return items[:limit], len(items) > limit
