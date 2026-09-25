"""
Change awareness: who needs to know a file changed.

`_record_change` already writes the `CodeChange` row. This module answers the
next question — which *live* runs care — and tells them through the steering
mailbox as a `kind='notice'` entry, which the `steering_node` renders as a
`system` context message rather than a user turn.

Correctness never rests on a notice arriving: an agent that misses one still
meets the stale-write guard at its next edit. Notices are a courtesy that
saves a round trip, not a lock anyone waits on.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def affected_threads(project_id: int, path: str, *,
                     writer_thread: str = '') -> list[str]:
    """Threads (other than the writer's) that have read `path` or claim it.

    Two ways a run is affected: its read set contains the path (it based work
    on text that just changed), or its task claims/`reads` globs cover it (it
    is about to). Readers never block, so this is the only thing that tells
    them — the stale guard tells them at a cost of a refused edit.
    """
    from workspaces import reads as _reads

    threads: list[str] = []
    seen: set[str] = set()

    for thread, files in list(_reads._reads.items()):
        if thread == writer_thread or thread in seen:
            continue
        if path in files:
            seen.add(thread)
            threads.append(thread)

    try:
        from agents.agent import tasks as _tasks
        from workspaces.leases import covers

        for bucket in _tasks._tasks.values():
            for record in bucket.values():
                if record.done.is_set():
                    continue
                if record.thread_id == writer_thread or record.thread_id in seen:
                    continue
                if record.project_id != project_id:
                    continue
                if any(covers(c, path) for c in record.claims):
                    seen.add(record.thread_id)
                    threads.append(record.thread_id)
    except Exception:  # noqa: BLE001 — awareness must never break a write
        logger.warning('[Awareness] Matcher failed', exc_info=True)

    return threads


def notify(project_id: int, path: str, *, by_label: str = '',
           writer_thread: str = '', writer_execution_id: str = '') -> int:
    """Tell every affected run (and the lead) that `path` changed.

    Returns how many runs were told. Best-effort throughout: a notice that
    fails to send is a saved round trip lost, never a write failed.
    """
    from chat.turn import steering

    label = (by_label or '').strip() or 'a worker'
    threads = affected_threads(
        project_id, path, writer_thread=writer_thread or '')
    for thread in threads:
        try:
            steering.post_notice(
                thread, f'{path} changed (by {label}); re-read it before your next edit.',
                path=path)
        except Exception:  # noqa: BLE001
            logger.warning('[Awareness] Notice to %s failed', thread,
                           exc_info=True)
    _tell_lead(project_id, path, label, writer_execution_id)
    return len(threads)


def _tell_lead(project_id: int, path: str, label: str,
               writer_execution_id: str) -> None:
    """The lead is always told, as a compact event rather than a notice.

    A notice is for the worker whose file changed; the lead gets one line in
    its `wait_tasks` event stream, and may choose to forward it (re-steering
    a test-writer whose target just changed shape, for example). Found by
    execution id: the bucket key *is* the lead's thread.
    """
    if not writer_execution_id:
        return
    try:
        from agents.agent import tasks as _tasks

        for parent_thread, bucket in _tasks._tasks.items():
            for record in bucket.values():
                if record.execution_id == writer_execution_id:
                    _tasks.post_event(
                        parent_thread,
                        f'change: {path} written by {record.label} '
                        f'(task {record.task_id})')
                    return
    except Exception:  # noqa: BLE001
        logger.warning('[Awareness] Lead event failed', exc_info=True)
