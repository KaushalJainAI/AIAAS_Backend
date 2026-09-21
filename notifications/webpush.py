"""
Web Push sender — the closed-browser OS notification channel.

The `ws/hitl/` socket only fires while a tab is open. A stored
`PushSubscription` lets the server reach the browser vendor's push service
when every tab is closed; the service worker (`better-n8n-frontend/public/sw.js`)
renders the OS notification and handles the click.

Best-effort by design: a dead push service must never break a sweep, a tool
call or a webhook. Unconfigured (no VAPID keys) means "skip silently", and
expired endpoints (HTTP 404/410 from the push service) are pruned.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - optional dependency missing
    webpush = None

    class WebPushException(Exception):
        """Fallback so `except WebPushException` stays valid without pywebpush."""


def is_configured() -> bool:
    """Whether VAPID keys are present, i.e. Web Push can actually send."""
    return bool(getattr(settings, 'VAPID_PUBLIC_KEY', '') and getattr(settings, 'VAPID_PRIVATE_KEY', ''))


def public_key() -> str:
    return getattr(settings, 'VAPID_PUBLIC_KEY', '') or ''


def send_web_push(user, *, title: str, body: str, action_url: str = '/inbox',
                  kind: str = 'notification') -> int:
    """
    Push to every stored subscription of `user`. Returns seats reached.

    Never raises — callers are sweeps, tool dispatch and webhooks.
    """
    from .models import PushSubscription

    if not title and not body:
        return 0
    if not is_configured():
        return 0

    subs = list(PushSubscription.objects.filter(user=user))
    if not subs:
        return 0
    if webpush is None:
        logger.warning("Web Push requested but pywebpush is not installed")
        return 0

    payload = json.dumps({
        'title': title,
        'body': body or '',
        'action_url': action_url or '/inbox',
        'kind': kind,
    }, separators=(',', ':'))
    vapid_claims = {'sub': getattr(settings, 'VAPID_SUBJECT', '') or 'mailto:no-reply@aiaas.local'}

    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    'endpoint': sub.endpoint,
                    'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                },
                data=payload,
                vapid_private_key=getattr(settings, 'VAPID_PRIVATE_KEY', ''),
                vapid_claims=vapid_claims,
            )
            sent += 1
        except WebPushException as exc:
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            if code in (404, 410):
                try:
                    sub.delete()
                    logger.info("Pruned expired push subscription %s for user %s", sub.pk, sub.user_id)
                except Exception:  # noqa: BLE001
                    logger.warning("Could not prune push subscription %s", sub.pk)
            else:
                logger.warning(
                    "Web Push failed for user %s (status=%s): %s", sub.user_id, code, exc,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Web Push failed for user %s: %s", sub.user_id, exc)
    return sent
