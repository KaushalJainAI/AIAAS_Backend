"""
Prune old chat checkpoints (Phase 4 of `docs/CONCURRENCY_LAG_FIX_PLAN.md`).

Chat keys the checkpointer by session id and nothing ever deleted those rows,
so every turn of every conversation adds a full state copy for ever (G7).
Agent runs do not need this: a finished run drops its thread (`forget_thread`)
and a live one resumes from its latest. A new chat turn also reads only the
latest — so keeping the latest few per chat thread loses nothing resumable.

What "chat thread" means: anything not starting with `agent-`. Agent runs own
the `agent-` prefix (`agents/agent/runtime.py`); chat uses the session id,
with `:nomem:` suffixed when memory is off. Worker threads (`sub-…`) fall on
the chat side of that line, which is safe for the same reason: resume reads
the latest, and the latest is always kept.

Only runs on the Postgres saver. The resume path needs, per pruned thread,
the checkpoint rows, their writes, and the blobs no kept checkpoint still
references — blobs are version-addressed and shared across a thread's history,
so they are deleted by exclusion, never by age. The SQLite dev saver has its
own file and no such pressure; the sweep reports `skipped` there rather than
speaking a second schema.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: Checkpoints kept per chat thread. A new turn reads only the latest; two
#: spare cover a resume that lands between the sweep and the next write.
DEFAULT_KEEP = 3

#: Threads examined per sweep, bounding how long one pass can take.
MAX_THREADS_PER_SWEEP = 200


def is_chat_thread(thread_id: str) -> bool:
    """Agent runs own the `agent-` prefix; everything else is pruned here."""
    return not (thread_id or '').startswith('agent-')


def plan_prune(entries: list[dict], keep: int) -> tuple[list[str], set[str]]:
    """Which checkpoint ids to delete, and which blob versions to keep.

    `entries` is one `(thread_id, checkpoint_ns)` group's checkpoints as
    `{checkpoint_id, step, versions}`. Ordering is by the saver-written
    `step`, never by id: checkpoint ids are not time-ordered. A checkpoint
    with no usable step is never pruned — an unorderable row might be the
    latest, and deleting the latest is the one unforgivable outcome here.

    Returns `(prune_ids, kept_versions)`: the ids to delete, and the union of
    `channel_versions` values of everything kept, so blob deletion can spare
    what the survivors still reference.
    """
    ordered = [e for e in entries if isinstance(e.get('step'), int)]
    if len(ordered) <= keep:
        kept_versions = {v for e in entries for v in (e.get('versions') or [])}
        return [], kept_versions
    ordered.sort(key=lambda e: e['step'], reverse=True)
    survivors, doomed = ordered[:keep], ordered[keep:]
    # Unknown-step rows survive with the survivors: see the docstring.
    surviving = survivors + [e for e in entries if not isinstance(e.get('step'), int)]
    kept_versions = {v for e in surviving for v in (e.get('versions') or [])}
    return [e['checkpoint_id'] for e in doomed], kept_versions


def _keep() -> int:
    try:
        return max(1, int(os.environ.get('CHAT_CHECKPOINT_KEEP', str(DEFAULT_KEEP))))
    except ValueError:
        return DEFAULT_KEEP


async def prune_chat_checkpoints(keep: int | None = None,
                                 dry_run: bool = False) -> dict:
    """Delete old chat checkpoints on the Postgres saver. See module docstring.

    Returns a tally (`threads_checked`, `threads_pruned`,
    `checkpoints_deleted`, `writes_deleted`, `blobs_deleted`, plus `status`
    when the sweep did not run). `dry_run` counts without deleting.
    """
    from chat.turn import checkpoints

    if checkpoints._configured() != 'postgres':
        return {'status': 'skipped',
                'reason': f'checkpointer is {checkpoints._configured()!r}, not postgres'}

    from chat.turn.agent import get_graph

    keep = keep if keep is not None else _keep()
    saver = get_graph().checkpointer
    tally: dict = {'threads_checked': 0, 'threads_pruned': 0,
                   'checkpoints_deleted': 0, 'writes_deleted': 0,
                   'blobs_deleted': 0}
    # Schema of langgraph-checkpoint-postgres 3.1.2 (pinned in
    # requirements.txt): checkpoints / checkpoint_writes / checkpoint_blobs.
    async with saver.conn.connection() as conn:
        cursor = await conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints "
            "WHERE thread_id NOT LIKE 'agent-%%' "
            "ORDER BY thread_id LIMIT %s",
            (MAX_THREADS_PER_SWEEP,),
        )
        thread_ids = [row[0] for row in await cursor.fetchall()]

        for thread_id in thread_ids:
            if not is_chat_thread(thread_id):
                continue
            tally['threads_checked'] += 1
            groups: dict[tuple[str, str], list[dict]] = {}
            async for tup in saver.alist(
                    {'configurable': {'thread_id': thread_id}}):
                cfg = (tup.config or {}).get('configurable', {})
                key = (thread_id, cfg.get('checkpoint_ns', ''))
                groups.setdefault(key, []).append({
                    'checkpoint_id': cfg.get('checkpoint_id', ''),
                    'step': (tup.metadata or {}).get('step'),
                    'versions': list(
                        (tup.checkpoint or {}).get('channel_versions', {}).values()),
                })
            for (tid, ns), entries in groups.items():
                prune_ids, kept_versions = plan_prune(entries, keep)
                if not prune_ids:
                    continue
                tally['threads_pruned'] += 1
                if dry_run:
                    tally['checkpoints_deleted'] += len(prune_ids)
                    continue
                cursor = await conn.execute(
                    "DELETE FROM checkpoint_writes "
                    "WHERE thread_id = %s AND checkpoint_ns = %s "
                    "AND checkpoint_id = ANY(%s)",
                    (tid, ns, prune_ids),
                )
                tally['writes_deleted'] += cursor.rowcount
                cursor = await conn.execute(
                    "DELETE FROM checkpoints "
                    "WHERE thread_id = %s AND checkpoint_ns = %s "
                    "AND checkpoint_id = ANY(%s)",
                    (tid, ns, prune_ids),
                )
                tally['checkpoints_deleted'] += cursor.rowcount
                if kept_versions:
                    # `version` is TEXT in the 3.1.2 schema while
                    # `channel_versions` values are ints — compare as text or
                    # Postgres refuses the query (and refusing is the good
                    # outcome; silently matching nothing would be worse).
                    kept = [str(v) for v in kept_versions]
                    cursor = await conn.execute(
                        "DELETE FROM checkpoint_blobs "
                        "WHERE thread_id = %s AND checkpoint_ns = %s "
                        "AND version <> ALL(%s)",
                        (tid, ns, kept),
                    )
                else:
                    cursor = await conn.execute(
                        "DELETE FROM checkpoint_blobs "
                        "WHERE thread_id = %s AND checkpoint_ns = %s",
                        (tid, ns),
                    )
                tally['blobs_deleted'] += cursor.rowcount
    if tally['threads_pruned']:
        logger.info('[Checkpoints] Pruned chat checkpoints: %s',
                    ', '.join(f'{v} {k}' for k, v in sorted(tally.items())))
    return tally
