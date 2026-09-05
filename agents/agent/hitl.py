"""
The approval queue: a paused agent run, as a row someone can answer later.

`HITLRequest` and the whole reminder engine behind it (`notifications/
reminders.py` — the escalation ladder, the hourly nudge, the daily digest) were
built, tested, and never reached: **nothing outside the test suite has ever
created a `HITLRequest`.** The DAG-era supervisor did, and it went with the DAG
product. So `agents/hitl/pending/` always answered with an empty list,
`notifications/signals.py` never armed a schedule, and the only thing a paused
agent produced was a WebSocket frame and one ad-hoc `Notification` row written
by `chat/turn/agent.py::_require_approval`.

That is why the builder's "Notify me when it stops to ask" toggle said *Coming
soon — you'll be notified for now even if this is off*: the notification existed
and the switch had nothing to switch.

This module is the missing write. One row, opened when a run pauses and closed
when the call is answered, is enough to light up everything already built.

Two rules it exists to hold:

* **The row is the queue, not the notification.** `notifyOnHitl` gates
  *delivery*, never this write. An agent whose notifications are off still has
  to appear in the Inbox, or the run pauses with no way to answer it and the
  toggle silently becomes "abandon this run".
* **Opening is idempotent.** `interrupt()` discards a node's writes and re-runs
  it from the top, so the same paused call can reach this more than once. Keyed
  on `(execution, node_id=call_id)`, a re-entry updates nothing and creates
  nothing — a second row would mean a second escalation ladder nudging about
  one question.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def open_request(execution_log, *, call_id: str, tool: str,
                       message: str, options: list | None = None,
                       title: str = '', detail: dict | None = None,
                       args: dict | None = None) -> None:
    """Record a paused tool call so it can be answered from the Inbox.

    `title` and `detail` come from `chat.tools.describe`; `args` is kept raw in
    `context_data` for the "show raw arguments" disclosure. The title used to
    be built here as `f'Approve {tool}?'`, which for the calls that matter most
    read `Approve mcp__7__send_email_ab12cd34?` — a question nobody can answer
    on its merits, about a tool whose real name is not even in the string.

    Best-effort: a run that has already stopped and asked must not then fail
    because the queue write did. The user still has the live WebSocket prompt,
    which is how every approval was answered before this row existed.
    """
    from agents.models import HITLRequest

    try:
        await HITLRequest.objects.aget_or_create(
            execution=execution_log,
            node_id=call_id,
            defaults={
                'user_id': execution_log.user_id,
                'request_type': 'approval',
                'title': (title or f'Approve {tool}?')[:200],
                'message': message,
                'options': options or [
                    {'label': 'Approve', 'value': 'approve'},
                    {'label': 'Reject', 'value': 'reject'},
                ],
                'context_data': {
                    'tool': tool,
                    'call_id': call_id,
                    # What the agent is asking to do, already rendered. Stored
                    # rather than recomputed at read time because naming the
                    # connection needs a row that may be gone by then, and an
                    # approval screen must describe the call as it was when it
                    # paused, not as the catalogue looks today.
                    'detail': detail or {},
                    'args': args or {},
                    # The two things answering it needs. `agents/{id}/approve/`
                    # is keyed on the thread, not on the execution, so a queue
                    # entry that did not carry it could be read and not acted
                    # on.
                    'thread_id': (execution_log.input_data or {}).get('thread_id', ''),
                    'agent_id': execution_log.subagent_id,
                },
            },
        )
    except Exception:
        logger.exception('[HITL] Could not queue approval for call %s', call_id)


async def resolve_request(*, thread_id: str, call_id: str, user_id: int,
                          status: str) -> None:
    """Close the queue entry for a call that has now been answered.

    Found by thread rather than by execution because that is what the approve
    and reject views hold, and deliberately without looking at the run's own
    status: `resume_agent_run` reopens the log, so a lookup that also required
    `status='paused'` would race the resume and leave the row pending — still
    in the Inbox, still nudging, for a question already answered.

    Closing it is what stops the reminders: `notifications/signals.py` cancels
    the escalation ladder on any status but `pending`.
    """
    from agents.models import HITLRequest

    try:
        request = await HITLRequest.objects.filter(
            user_id=user_id,
            node_id=call_id,
            status='pending',
            # Indexed column rather than the JSON path — this is on the
            # click path for every approval and rejection.
            execution__thread_id=thread_id,
        ).afirst()
        if request is None:
            # Normal for a chat approval, and for any run that paused before
            # this queue existed. Not an error, and not worth a warning on a
            # path the user is watching.
            return
        request.status = status
        from django.utils import timezone

        request.responded_at = timezone.now()
        request.response = {'action': status}
        await request.asave(
            update_fields=['status', 'responded_at', 'response', 'updated_at']
        )
    except Exception:
        logger.exception('[HITL] Could not close approval for call %s', call_id)
