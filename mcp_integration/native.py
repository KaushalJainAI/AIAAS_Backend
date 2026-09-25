"""
Native connectors: a Connections card whose tools are ours, not a subprocess's.

A card used to be a process. Every curated connector was an `npx` MCP server —
70-150 MB of Node each — on a box whose web container has 384 MB, and the
memory budget that stopped them killing daphne also meant their tool lists
were never built, so the tools silently never appeared (2026-09-17). Gmail,
Drive, Sheets and Calendar are now `type='native'` rows whose tools live in
`chat/tools/google/` and call Google's REST APIs from this process.

The row stays because everything a user and an agent *govern* a connector with
is keyed on it: the Connections switch (`MCPServerPreference`), the credential
the card asks for (`required_credential_types`), and an agent's `connectors`
scope (a list of server ids). Only the tool source changed, so none of those
had to.

**A tool is live when its card is.** That means three things, all of which
`live_native_connectors` checks together, because any one missing makes an
offered tool that can only refuse:

* the row is enabled platform-wide and not switched off by this user,
* the user holds every credential type the row requires,
* and there is a registered tool naming the row's `icon_slug`.

Presence of a credential, not its validity: whether a token still refreshes
is only known by using it, and the tool reports `credential_invalid` when it
does not. Refreshing tokens to decide what to *offer* would put a network call
in front of every turn.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

NATIVE_TYPE = "native"


def _live_sync(user_id: int | None) -> dict[str, int]:
    from credentials.models import Credential

    from .client import _visible_servers_queryset

    if not user_id:
        return {}
    rows = list(
        _visible_servers_queryset(user_id, enabled_only=True)
        .filter(type=NATIVE_TYPE)
        .values_list("id", "icon_slug", "required_credential_types")
    )
    if not rows:
        return {}
    held = set(
        Credential.objects.filter(user_id=user_id, is_active=True)
        .values_list("credential_type__slug", flat=True)
    )
    live: dict[str, int] = {}
    for server_id, slug, required in rows:
        if not slug:
            continue
        if all(r in held for r in (required or [])):
            live.setdefault(slug, server_id)
    return live


def _server_ids_sync(user_id: int | None) -> dict[str, int]:
    from .client import _visible_servers_queryset

    if not user_id:
        return {}
    ids: dict[str, int] = {}
    for server_id, slug in (_visible_servers_queryset(user_id, enabled_only=False)
                            .filter(type=NATIVE_TYPE)
                            .values_list("id", "icon_slug")):
        if slug:
            ids.setdefault(slug, server_id)
    return ids


async def native_server_ids(user_id: int | None) -> dict[str, int]:
    """`icon_slug -> server id` for every native card, live or not.

    For an eval world, whose simulated connector tools need no card switched
    on and no credential — but must still be judged against the agent's
    connector *scope*, which is keyed by these ids. A failure answers "none",
    so a scoped tool is refused rather than let through unchecked.
    """
    try:
        return await sync_to_async(_server_ids_sync)(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve native server ids for user %s", user_id, exc_info=True)
        return {}


async def live_native_connectors(user_id: int | None) -> dict[str, int]:
    """`icon_slug -> server id` for every native card live for this user.

    A failure answers "none are live" — the opposite of the tool-library
    overlay's rule, and deliberately: a failed overlay read must not take tools
    *away*, while a failed read here must not hand out tools that reach a
    user's mailbox without the switch that governs them having been checked.
    """
    try:
        return await sync_to_async(_live_sync)(user_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not resolve native connectors for user %s", user_id, exc_info=True)
        return {}


def tools_for_row(server) -> list[dict]:
    """The `tools/` listing for a native row, from the registry — no client."""
    from chat.tools.registry import connector_tool_names, get

    names = sorted(connector_tool_names(server.icon_slug or ""))
    listing = []
    for name in names:
        fn = get(name).schema["function"]
        listing.append({
            "name": name,
            "description": fn.get("description", ""),
            "inputSchema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return listing
