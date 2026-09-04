"""
Configuration history for an agent.

`SubAgent` carried only `updated_at`, so "this agent got worse last Tuesday" had
no answer: there was no record of what it had been before. A revision is a
snapshot of the configuration plus the diff that produced it, and
`ExecutionLog.revision` pins every run to the one it executed under. Together
those two facts turn tuning an agent from guesswork into something you can read.

**The snapshot is `AgentSerializer.to_config(agent)`**, the same flat
`AgentConfig` dict the builder already speaks. Serialising the columns a second
time here would create a second mapping that could drift from the one the UI
renders — and a config history that disagrees with the config screen is worse
than none.

**A revision is only written when something changed.** The builder PATCHes on
most interactions, and a timeline containing forty identical entries is not a
record of decisions. `record()` returns None when the diff is empty.
"""
from __future__ import annotations

import logging
from typing import Any

from workflow_backend.thresholds import REVISION_VALUE_CHAR_LIMIT

from .models import SubAgentRevision

logger = logging.getLogger(__name__)

#: Keys in the config snapshot that say nothing about behaviour. Diffing them
#: would mint a revision on every save, since `updated_at` moves every time.
_IGNORED_KEYS = frozenset({'id', 'created_at', 'updated_at', 'runs', 'unattended',
                           'spend', 'status'})

#: How a changed key is described in the one-line summary. Anything absent falls
#: back to the key itself, so a new config field still reads sensibly.
_LABELS: dict[str, str] = {
    'brief': 'brief',
    'provider': 'provider',
    'model': 'model',
    'temperature': 'temperature',
    'tools': 'tools',
    'connectors': 'connectors',
    'knowledgeBases': 'knowledge bases',
    'skills': 'skills',
    'autonomy': 'autonomy',
    'spendCapRupees': 'spend cap',
    'egress': 'egress',
    'allowUnattended': 'unattended',
    'schedule': 'schedule',
    'fileAccess': 'file access',
    'memoryMb': 'memory',
}


def snapshot(agent) -> dict[str, Any]:
    """The agent's current configuration, as the builder sees it.

    Imported inside the function because `agents.views.agents` imports this
    module — taking it at module scope would close the cycle.
    """
    from agents.views.agents import AgentSerializer

    config = AgentSerializer.to_config(agent)
    # Datetimes are not JSON-serialisable and carry no configuration meaning.
    return {k: v for k, v in config.items() if k not in _IGNORED_KEYS}


def _clip(value: Any) -> Any:
    """Bound one side of a diff. A brief has no length limit of its own."""
    if isinstance(value, str) and len(value) > REVISION_VALUE_CHAR_LIMIT:
        return value[:REVISION_VALUE_CHAR_LIMIT] + f'… [{len(value)} chars]'
    return value


def diff(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    """What changed between two snapshots, as `{key: {'from': …, 'to': …}}`.

    A key present in only one side counts as a change; that is how a new config
    field first shows up, and recording it is more useful than hiding it.
    """
    before = before or {}
    changed: dict[str, Any] = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old != new:
            changed[key] = {'from': _clip(old), 'to': _clip(new)}
    return changed


def summarise(changed: dict[str, Any]) -> str:
    """A one-line, human-scannable description of a diff.

    Reads as a list of what moved, not a count: "model, autonomy, tools" tells
    you whether to look, where "3 fields changed" never does.
    """
    if not changed:
        return 'No changes'
    labels = [_LABELS.get(key, key) for key in changed]
    if len(labels) <= 4:
        return ', '.join(labels)
    return ', '.join(labels[:3]) + f' and {len(labels) - 3} more'


def record(agent, *, user=None, source: str = 'update') -> SubAgentRevision | None:
    """Write a revision for `agent` if its configuration actually changed.

    Returns the new revision, or None when nothing moved. Callers should not
    treat None as failure — it is the common case on a no-op save.

    Not wrapped in its own transaction: callers run inside the request's atomic
    block, and a revision that survives a rolled-back agent save would describe
    a configuration that never existed.
    """
    latest = (
        SubAgentRevision.objects
        .filter(subagent=agent)
        .order_by('-number')
        .first()
    )
    after = snapshot(agent)
    changed = diff(latest.config if latest else None, after)
    if not changed and latest is not None:
        return None

    return SubAgentRevision.objects.create(
        subagent=agent,
        user=user if (user is not None and getattr(user, 'is_authenticated', False)) else None,
        number=(latest.number + 1) if latest else 1,
        config=after,
        diff=changed,
        summary=summarise(changed) if latest else 'Created',
        source=source,
    )


def current(agent) -> SubAgentRevision | None:
    """The revision a run starting now would execute under.

    Mints one lazily for an agent that predates revision tracking, so its first
    run still has a configuration to point at. Doing it here rather than in a
    data migration means the snapshot is taken by the same code path every other
    revision uses, instead of by a second one written against historical models.
    """
    latest = (
        SubAgentRevision.objects
        .filter(subagent=agent)
        .order_by('-number')
        .first()
    )
    if latest is not None:
        return latest
    try:
        return record(agent, source='backfill')
    except Exception:  # noqa: BLE001 — observability must never fail a run
        logger.exception('[Revisions] Could not backfill a revision for agent %s',
                         agent.id)
        return None
