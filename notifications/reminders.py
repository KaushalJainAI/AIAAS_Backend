"""
HITL reminder engine.

One sweep drives three independent nudge channels, deliberately split across
different transports so that "the agent is blocked" never turns into an inbox
full of mail:

    escalation   device ping at +0, +1h, +1d for each unanswered request
    hourly       optional standing device ping while anything is pending
    daily digest one email + in-app roll-up, at a wall-clock time the user picks

Only the digest is allowed to send email, and `last_digest_sent_on` caps it at
one per calendar day in the user's own timezone. Escalation and hourly pass
``send_email=False`` explicitly so a future change to the global email gate
cannot start mailing them.

`run_reminder_sweep()` is the single entry point. It is called both by the
Celery beat task in `tasks.py` and by `manage.py send_hitl_reminders`, so the
behaviour is identical whether or not a broker is running.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dt_time, timedelta, timezone as dt_timezone

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9 is unsupported elsewhere
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception


# Fire the hourly nudge once the previous one is this old. Slightly under an
# hour so a sweep landing at 59m does not defer the ping a whole sweep period.
HOURLY_TOLERANCE = timedelta(minutes=59)


# After this long with no answer a pending request is treated as abandoned and
# cancelled, so it stops nudging and stops holding its run open.
#
# Deliberately *not* `HITLRequest.timeout_seconds`, whose default is 300s: that
# field predates the escalation ladder, and honouring it would cancel every
# request five minutes in — before the +1h nudge the ladder exists to send.
# `timeout_seconds` and `auto_action` therefore remain unused until someone
# decides what a per-request timeout should mean alongside the ladder; this
# backstop only catches requests nobody will ever answer.
ABANDON_AFTER = timedelta(days=getattr(settings, 'HITL_ABANDON_AFTER_DAYS', 7))


# ---------------------------------------------------------------------------
# preferences
# ---------------------------------------------------------------------------

def get_preferences(user):
    """Preferences for `user`, created with defaults on first access."""
    from .models import NotificationPreference

    prefs, _ = NotificationPreference.objects.get_or_create(user=user)
    return prefs


def _zone(prefs):
    """Resolve the user's tzinfo, falling back to UTC on a bad name."""
    name = prefs.effective_timezone
    if ZoneInfo is None:
        return dt_timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        logger.warning("Unknown timezone %r for user %s; using UTC", name, prefs.user_id)
        return dt_timezone.utc


def _in_quiet_hours(prefs, now: datetime) -> bool:
    """
    Whether `now` falls inside the user's quiet window.

    Suppresses the OS-level ping only — the in-app row is still written and the
    escalation ladder still advances, so a request cannot get stuck waiting for
    a nudge that quiet hours permanently swallow.
    """
    if not prefs.quiet_hours_enabled:
        return False

    local = now.astimezone(_zone(prefs)).time()
    start, end = prefs.quiet_hours_start, prefs.quiet_hours_end
    if start == end:
        return False
    if start < end:
        return start <= local < end
    # Window wraps midnight (e.g. 22:00 → 08:00)
    return local >= start or local < end


# ---------------------------------------------------------------------------
# delivery
# ---------------------------------------------------------------------------

def push_device_notification(user_id: int, payload: dict) -> bool:
    """
    Push an OS-level notification request over the per-user HITL socket.

    The frontend raises a browser Notification and BrowserOS raises a desktop
    one; both listen on the same `hitl_{user_id}` group. Best-effort — a dead
    channel layer must never break the sweep or HITL creation.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    layer = get_channel_layer()
    if layer is None:
        return False
    try:
        async_to_sync(layer.group_send)(
            f"hitl_{user_id}",
            {'type': 'hitl.reminder', 'reminder': payload},
        )
        return True
    except Exception as exc:  # channel layer down, no Redis, no running loop
        logger.warning("Device notification push failed for user %s: %s", user_id, exc)
        return False


def _notify_device(prefs, *, notif_type: str, title: str, message: str, data: dict) -> None:
    """In-app row always; OS ping when enabled and outside quiet hours."""
    from .utils import create_notification

    # send_email=False is load-bearing: email belongs to the digest alone.
    create_notification(
        user=prefs.user,
        type=notif_type,
        title=title,
        message=message,
        data=data,
        send_email=False,
    )

    if prefs.device_notifications_enabled and not _in_quiet_hours(prefs, timezone.now()):
        push_device_notification(prefs.user_id, {
            'kind': notif_type,
            'title': title,
            'body': message,
            **data,
        })


# ---------------------------------------------------------------------------
# escalation ladder
# ---------------------------------------------------------------------------

def deliver_escalation(schedule, prefs=None, now=None) -> bool:
    """
    Send the nudge for `schedule`'s current stage and arm the next one.

    Returns whether anything was sent.
    """
    from .models import HITLReminderSchedule

    now = now or timezone.now()
    prefs = prefs or get_preferences(schedule.user)
    request = schedule.hitl_request

    if request.status != 'pending':
        schedule.cancel()
        return False
    if schedule.is_exhausted:
        schedule.cancel()
        return False
    if not prefs.hitl_escalation_enabled:
        schedule.cancel()
        return False

    stage = schedule.stage
    if stage == 0:
        title = f"Agent needs you: {request.title}"
    else:
        waited = HITLReminderSchedule.STAGE_LABELS.get(stage, 'a while')
        title = f"Still waiting after {waited}: {request.title}"

    _notify_device(
        prefs,
        notif_type='hitl_request' if stage == 0 else 'hitl_reminder',
        title=title,
        message=request.message,
        data={
            'request_id': str(request.request_id),
            'request_type': request.request_type,
            'execution_id': str(request.execution.execution_id) if request.execution_id else None,
            'node_id': request.node_id,
            'stage': stage,
            'action_url': '/inbox',
        },
    )
    schedule.advance(now)
    return True


def _sweep_escalations(now: datetime) -> int:
    from .models import HITLReminderSchedule

    # Self-heal: a request answered while the sweep was down leaves an armed
    # schedule behind. The signal normally clears these; this covers the rest.
    HITLReminderSchedule.objects.filter(next_due_at__isnull=False).exclude(
        hitl_request__status='pending'
    ).update(next_due_at=None)

    due = (
        HITLReminderSchedule.objects
        .select_related('hitl_request', 'hitl_request__execution', 'user')
        .filter(next_due_at__isnull=False, next_due_at__lte=now, hitl_request__status='pending')
    )

    sent = 0
    for schedule in due:
        try:
            if deliver_escalation(schedule, now=now):
                sent += 1
        except Exception as exc:
            logger.exception("Escalation delivery failed for schedule %s: %s", schedule.pk, exc)
    return sent


# ---------------------------------------------------------------------------
# optional hourly reminder
# ---------------------------------------------------------------------------

def pending_for_nudges(user):
    """Pending requests the user has asked to be *pushed* about.

    Excludes agents whose `guardrails['notifyOnHitl']` is off, and *only* those:
    the key is absent on every agent saved before it was read, and absent has to
    mean "yes" — it is the builder's default and the behaviour those agents
    already have.

    Spelled as three positive alternatives rather than the obvious
    `exclude(...=False)`, which silently gets this backwards. On a JSON key
    path, `NOT (key = False)` is SQL NULL when the key is missing, and NULL is
    not TRUE — so the row is dropped. `exclude` would therefore have silenced
    exactly the agents that never chose to be silent, which is the one outcome
    this whole feature exists to avoid.

    The digest deliberately does **not** use this. Escalation and the hourly
    nudge are pushes, and "don't notify me about this agent" is exactly a
    request not to be pushed; the digest is a once-a-day roll-up of everything
    outstanding, and one that hid pending work would be worse than no digest.
    """
    from django.db.models import Q

    from agents.models import HITLRequest

    return (
        HITLRequest.objects
        .filter(user=user, status='pending')
        .filter(
            Q(execution__subagent__guardrails__notifyOnHitl=True)
            # Key absent — an agent that predates the setting.
            | Q(execution__subagent__guardrails__notifyOnHitl__isnull=True)
            # No agent behind the run at all (a deleted one, or a historical
            # row): there is nothing to have opted out, so it still counts.
            | Q(execution__subagent__isnull=True)
        )
    )


def _sweep_hourly(now: datetime) -> int:
    from .models import NotificationPreference

    cutoff = now - HOURLY_TOLERANCE
    candidates = (
        NotificationPreference.objects
        .select_related('user')
        .filter(hourly_reminders_enabled=True)
    )

    sent = 0
    for prefs in candidates:
        if prefs.last_hourly_sent_at and prefs.last_hourly_sent_at > cutoff:
            continue
        # Quiet hours skip the tick outright rather than burning the timer, so
        # the first ping after the window closes is still an hour's worth.
        if _in_quiet_hours(prefs, now):
            continue

        pending = pending_for_nudges(prefs.user)
        count = pending.count()
        if not count:
            continue

        oldest = pending.order_by('created_at').first()
        try:
            _notify_device(
                prefs,
                notif_type='hitl_reminder',
                title=f"{count} request{'s' if count != 1 else ''} still waiting on you",
                message=(
                    f"Your agents cannot finish until you respond. Oldest: "
                    f"“{oldest.title}”."
                ),
                data={'pending_count': count, 'action_url': '/inbox', 'reason': 'hourly'},
            )
        except Exception as exc:
            logger.exception("Hourly reminder failed for user %s: %s", prefs.user_id, exc)
            continue

        prefs.last_hourly_sent_at = now
        prefs.save(update_fields=['last_hourly_sent_at', 'updated_at'])
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# daily digest — the only channel that emails
# ---------------------------------------------------------------------------

def _digest_body(requests) -> str:
    lines = [
        f"{len(requests)} request{'s' if len(requests) != 1 else ''} "
        f"{'are' if len(requests) != 1 else 'is'} waiting on you:",
        "",
    ]
    for req in requests[:20]:
        age = timezone.now() - req.created_at
        hours = int(age.total_seconds() // 3600)
        waited = f"{hours}h" if hours < 48 else f"{hours // 24}d"
        lines.append(f"  • [{req.request_type}] {req.title} — waiting {waited}")
    if len(requests) > 20:
        lines.append(f"  …and {len(requests) - 20} more.")
    lines += ["", "Respond in your Inbox to let these runs finish."]
    return "\n".join(lines)


def _sweep_daily_digests(now: datetime) -> int:
    from agents.models import HITLRequest

    from .models import NotificationPreference
    from .utils import create_notification

    candidates = (
        NotificationPreference.objects
        .select_related('user')
        .filter(daily_digest_enabled=True)
    )

    sent = 0
    for prefs in candidates:
        local = now.astimezone(_zone(prefs))
        local_date = local.date()

        # The hard once-per-calendar-day cap on email.
        if prefs.last_digest_sent_on == local_date:
            continue
        # Not yet the chosen time. A sweep that was down over the slot catches
        # up at its next run rather than skipping the day.
        if local.time() < (prefs.daily_digest_time or dt_time(9, 0)):
            continue

        pending = list(
            HITLRequest.objects
            .filter(user=prefs.user, status='pending')
            .order_by('created_at')
        )

        # Claim the day either way: with nothing pending at the chosen time,
        # that day's digest is spent. Otherwise a request arriving at 22:00
        # would trigger a "daily" digest at 22:00.
        prefs.last_digest_sent_on = local_date
        prefs.save(update_fields=['last_digest_sent_on', 'updated_at'])

        if not pending:
            continue

        try:
            # send_email is left to the global NOTIFICATIONS_EMAIL_* gate; this
            # is the one notification type that is meant to reach the inbox.
            create_notification(
                user=prefs.user,
                type='hitl_digest',
                title=f"{len(pending)} request{'s' if len(pending) != 1 else ''} waiting on you",
                message=_digest_body(pending),
                data={
                    'pending_count': len(pending),
                    'action_url': '/inbox',
                    'request_ids': [str(r.request_id) for r in pending[:50]],
                },
            )
            if prefs.device_notifications_enabled:
                push_device_notification(prefs.user_id, {
                    'kind': 'hitl_digest',
                    'title': f"{len(pending)} request{'s' if len(pending) != 1 else ''} waiting on you",
                    'body': 'Open your Inbox to unblock your agents.',
                    'pending_count': len(pending),
                    'action_url': '/inbox',
                })
            sent += 1
        except Exception as exc:
            logger.exception("Daily digest failed for user %s: %s", prefs.user_id, exc)
    return sent


# ---------------------------------------------------------------------------
# abandonment
# ---------------------------------------------------------------------------

def _sweep_abandoned(now: datetime) -> int:
    """Cancel requests nobody is ever going to answer.

    Without this a pending request is immortal: it nudges on the ladder, joins
    every daily digest, and — once triggers exist — holds its run open while
    the next scheduled tick starts another one on top. Cancelling is the honest
    outcome; auto-approving an abandoned request would grant a permission the
    user never gave.
    """
    from agents.models import HITLRequest

    cutoff = now - ABANDON_AFTER
    return HITLRequest.objects.filter(
        status='pending', created_at__lt=cutoff,
    ).update(status='cancelled', responded_at=now)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def run_reminder_sweep(now: datetime | None = None) -> dict:
    """
    Fire every nudge that is due. Idempotent per sweep: each channel records
    what it sent, so running this more often than scheduled is harmless.
    """
    now = now or timezone.now()
    result = {
        # Abandonment first: a request cancelled by this sweep must not also be
        # nudged about by the same sweep.
        'abandoned': _sweep_abandoned(now),
        'escalations': _sweep_escalations(now),
        'hourly': _sweep_hourly(now),
        'digests': _sweep_daily_digests(now),
    }
    if any(result.values()):
        logger.info(
            "HITL reminder sweep: %s abandoned, %s escalation(s), %s hourly, "
            "%s digest(s)",
            result['abandoned'], result['escalations'], result['hourly'],
            result['digests'],
        )
    return result
