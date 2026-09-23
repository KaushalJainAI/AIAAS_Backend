"""
User-asked reminders: at a time, or on a heartbeat.

`notify_user` is the immediate ping — a run telling its owner something now.
This is the other half: "tell me when X finishes" (set for a time) and "tell
me every morning / every hour" (a heartbeat). Both are created through the
`schedule_notification` tool, which only ever records timing the user stated.

Delivery mirrors `notify_user` on purpose: an in-app `scheduled_reminder`
row always, the device ping unless quiet hours say otherwise, web push behind
the same device toggle — and never email (the digest owns that channel).

`run_scheduled_sweep()` is the single entry point, called by the Celery beat
task (`tasks.sweep_scheduled`) and by `manage.py send_scheduled_notifications`,
so behaviour is identical with or without a broker — the same split every
other sweep in this codebase keeps.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

logger = logging.getLogger(__name__)

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9 is unsupported elsewhere
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

#: How far ahead a reminder may be set. A year is generosity, not a promise —
#: the point is refusing "in 100 years" typos, not constraining real plans.
MAX_AHEAD = timedelta(days=365)

#: Batch cap per sweep. Reminders are cheap rows; the cap bounds one sweep's
#: work, and anything left fires on the next minute's pass.
SWEEP_BATCH = 100


def parse_run_at(raw: str, user) -> datetime:
    """User-supplied time → an aware UTC datetime. Raises `ValueError`.

    A naive time is read in the user's own timezone (their preference, else
    profile, else UTC) — "9am tomorrow" means their 9am, the same rule cron
    expressions follow for triggers. An aware time is honoured as given.
    """
    from .reminders import get_preferences

    text = (raw or '').strip()
    if not text:
        raise ValueError('Give a date and time, e.g. 2026-09-24T09:00:00+05:30.')
    candidate = text
    if candidate.endswith(('Z', 'z')):
        candidate = candidate[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError(
            'That time did not parse. Use ISO format, e.g. '
            '2026-09-24T09:00:00+05:30.')
    if parsed.tzinfo is None:
        prefs = get_preferences(user)
        name = prefs.effective_timezone
        try:
            zone = ZoneInfo(name) if ZoneInfo is not None else dt_timezone.utc
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            zone = dt_timezone.utc
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(dt_timezone.utc)


def validate_schedule(run_at: datetime, repeat: str, now: datetime) -> None:
    """Refuse what cannot be a reminder anyone meant. Raises `ValueError`."""
    from .models import ScheduledNotification

    kinds = [key for key, _ in ScheduledNotification.REPEAT_CHOICES]
    if repeat not in kinds:
        raise ValueError(f'Repeat must be one of {", ".join(kinds)}.')
    if run_at <= now:
        raise ValueError('That time is in the past — reminders only run forward.')
    if run_at - now > MAX_AHEAD:
        raise ValueError('That is more than a year out; reminders run within a year.')


def schedule_for_user(user, title: str, message: str, run_at: datetime,
                       repeat: str = 'none', data: dict | None = None,
                       send_email: bool = False):
    """Create a live reminder, enforcing the per-user cap. Raises `ValueError`."""
    from .models import ScheduledNotification

    live = ScheduledNotification.objects.filter(
        user=user, active=True, next_run_at__isnull=False).count()
    if live >= ScheduledNotification.MAX_ACTIVE_PER_USER:
        raise ValueError(
            f'You already have {live} live reminders (the limit is '
            f'{ScheduledNotification.MAX_ACTIVE_PER_USER}). Cancel one first.')
    return ScheduledNotification.objects.create(
        user=user, title=title[:120], message=message[:1000],
        data=data or {}, repeat=repeat, next_run_at=run_at, active=True,
        send_email=bool(send_email))


def _deliver(reminder, now: datetime) -> None:
    """One firing: the feed row, the device ping, the push twin.

    Email only when the reminder opted in — explicit user consent, which is
    what separates this from every system nudge that never emails. A
    misconfigured mailer degrades (logged thread, row still written), never
    fails the firing.
    """
    from .models import NotificationPreference
    from .reminders import _in_quiet_hours
    from .utils import create_notification
    from .webpush import send_web_push

    create_notification(
        reminder.user, 'scheduled_reminder',
        reminder.title, reminder.message,
        data=reminder.data or {},
        send_email=True if reminder.send_email else False)
    try:
        prefs = NotificationPreference.objects.filter(
            user=reminder.user).first()
        if prefs is None or prefs.device_notifications_enabled:
            if prefs is not None and _in_quiet_hours(prefs, now):
                return
            send_web_push(
                reminder.user, title=reminder.title, body=reminder.message,
                action_url=(reminder.data or {}).get('action_url') or '/runs',
                kind='scheduled_reminder')
    except Exception:
        logger.exception('[Scheduled] web push failed')


def run_scheduled_sweep(now: datetime | None = None) -> dict[str, int]:
    """Fire every due reminder. Returns `{'sent': n}`.

    One-shot reminders go quiet after firing; repeating ones advance from the
    previous due time so a late sweep cannot shift the heartbeat. Each firing
    is independent — one bad row logs and the sweep moves on.
    """
    from .models import ScheduledNotification

    now = now or timezone.now()
    due = list(
        ScheduledNotification.objects
        .filter(active=True, next_run_at__isnull=False, next_run_at__lte=now)
        .select_related('user')
        .order_by('next_run_at')[:SWEEP_BATCH])
    sent = 0
    for reminder in due:
        try:
            _deliver(reminder, now)
            offset = ScheduledNotification.REPEAT_OFFSETS.get(reminder.repeat)
            if offset:
                reminder.next_run_at = reminder.next_run_at + timedelta(seconds=offset)
                reminder.last_sent_at = now
                reminder.times_sent += 1
                reminder.save(update_fields=[
                    'next_run_at', 'last_sent_at', 'times_sent', 'updated_at'])
            else:
                reminder.active = False
                reminder.next_run_at = None
                reminder.last_sent_at = now
                reminder.times_sent += 1
                reminder.save(update_fields=[
                    'active', 'next_run_at', 'last_sent_at', 'times_sent',
                    'updated_at'])
            sent += 1
        except Exception:
            logger.exception('[Scheduled] firing reminder %s failed', reminder.id)
    return {'sent': sent}
