"""
Open, reuse and retire `BrowserSession` rows.

One user per profile, one domain per profile: a session for (user, domain) is
reused while open, so a login survives across turns. Anything idle past
`BROWSER_SESSION_IDLE_SECONDS` or older than `BROWSER_SESSION_MAX_SECONDS` is
closed by the sweep (beat task + management command, the usual split).
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import BrowserSession


def _idle_seconds() -> int:
    try:
        return int(getattr(settings, 'BROWSER_SESSION_IDLE_SECONDS', 600))
    except (TypeError, ValueError):
        return 600


def _max_seconds() -> int:
    try:
        return int(getattr(settings, 'BROWSER_SESSION_MAX_SECONDS', 3600))
    except (TypeError, ValueError):
        return 3600


def normalise_domain(domain: str) -> str:
    return str(domain or '').lower().strip().lstrip('.').rstrip('.')


def ensure(user, domain: str) -> BrowserSession:
    """The open session for (user, domain), or a fresh one."""
    from browsing.engine import host_of

    domain = normalise_domain(host_of(domain) if '://' in domain else domain)
    if not domain:
        raise ValueError('A session needs a domain.')
    now = timezone.now()
    existing = (
        BrowserSession.objects
        .filter(user=user, domain=domain, status='open', expires_at__gt=now)
        .order_by('-last_used_at')
        .first()
    )
    if existing is not None:
        if now - existing.last_used_at <= timedelta(seconds=_idle_seconds()):
            existing.save(update_fields=['last_used_at'])
            return existing
        existing.status = 'expired'
        existing.save(update_fields=['status'])
    return BrowserSession.objects.create(
        user=user, domain=domain,
        expires_at=now + timedelta(seconds=_max_seconds()),
    )


def touch(session: BrowserSession) -> BrowserSession:
    session.save(update_fields=['last_used_at'])
    return session


def close(session: BrowserSession, *, expired: bool = False) -> None:
    session.status = 'expired' if expired else 'closed'
    session.save(update_fields=['status'])


def sweep() -> dict[str, int]:
    """Close idle and over-age sessions. Returns counts by outcome."""
    now = timezone.now()
    idle_before = now - timedelta(seconds=_idle_seconds())
    idle = BrowserSession.objects.filter(
        status='open', last_used_at__lt=idle_before,
    ).update(status='expired')
    aged = BrowserSession.objects.filter(
        status='open', expires_at__lte=now,
    ).update(status='expired')
    return {'idle_expired': idle, 'aged_expired': aged}
