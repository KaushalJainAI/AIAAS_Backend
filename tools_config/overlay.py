"""
Reading the overlay on the hot path.

Every tool listing and every dispatch consults this, so it is a cache with a
database behind it rather than a query. One entry per user holds the whole
overlay — a dict of the rows that exist — because the question is always "what
did this user change?", never "who changed `read_url`?", and a per-tool key
would turn one lookup into twenty on a listing.

Invalidation is a delete on write (`signals.py`), with a TTL underneath as the
backstop for the case the delete cannot reach: `LocMemCache` in local dev is
per-process, so a write served by one worker leaves another worker's copy
stale. The TTL bounds that to a minute rather than to a restart.
"""
from __future__ import annotations

from asgiref.sync import sync_to_async
from django.core.cache import cache

from .settings_schema import defaults_for

#: Bumped when the shape below changes, so a deploy cannot read yesterday's
#: entries as if they were today's.
CACHE_VERSION = 'v1'
CACHE_TTL = 60  # seconds


def _cache_key(user_id: int) -> str:
    return f'tools_config:{CACHE_VERSION}:{user_id}'


def _load(user_id: int) -> dict[str, dict]:
    from .models import ToolConfig

    rows = ToolConfig.objects.filter(user_id=user_id).values(
        'tool_name', 'enabled', 'config',
    )
    return {
        r['tool_name']: {'enabled': r['enabled'], 'config': r['config'] or {}}
        for r in rows
    }


def overlay(user_id: int | None) -> dict[str, dict]:
    """Every row this user has, keyed by tool name. `{}` for an anonymous caller."""
    if not user_id:
        return {}
    key = _cache_key(user_id)
    cached = cache.get(key)
    if cached is not None:
        return cached
    data = _load(user_id)
    cache.set(key, data, CACHE_TTL)
    return data


#: Thread-sensitive on purpose (the default): this reads the ORM, so it has
#: to run on the connection the surrounding request or `background.spawn()`
#: context already owns. Pushed onto a free thread it opens a second
#: connection, which under SQLite is a locked table and under Postgres is a
#: connection nobody closes.
aoverlay = sync_to_async(overlay)


def invalidate(user_id: int) -> None:
    cache.delete(_cache_key(user_id))


def disabled_names(user_id: int | None) -> frozenset[str]:
    """Tools this user has switched off. Absent row means on."""
    return frozenset(
        name for name, row in overlay(user_id).items() if not row.get('enabled', True)
    )


async def adisabled_names(user_id: int | None) -> frozenset[str]:
    rows = await aoverlay(user_id)
    return frozenset(
        name for name, row in rows.items() if not row.get('enabled', True)
    )


def limits(user_id: int | None, tool_name: str) -> dict[str, int]:
    """This tool's knobs for this user: declared defaults with overrides applied."""
    merged = defaults_for(tool_name)
    row = overlay(user_id).get(tool_name)
    if row:
        for key, value in (row.get('config') or {}).items():
            if key in merged and isinstance(value, int) and not isinstance(value, bool):
                merged[key] = value
    return merged


async def alimit(context: dict | None, tool_name: str, key: str) -> int:
    """The one call a tool makes: `await alimit(context, 'read_url', 'charLimit')`.

    Takes the tool's own `context` rather than a user id so the call site is a
    single expression, and falls back to the declared default whenever there is
    no user or the lookup fails — a tool must not fail because a preference
    could not be read.
    """
    default = defaults_for(tool_name).get(key)
    if default is None:  # undeclared knob: a bug at the call site, not at runtime
        raise KeyError(f'{tool_name} declares no setting {key!r}')
    user_id = (context or {}).get('user_id')
    if not user_id:
        return default
    try:
        values = await sync_to_async(limits)(user_id, tool_name)
    except Exception:  # noqa: BLE001 — a preference read must never break a tool
        return default
    return values.get(key, default)
