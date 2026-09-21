"""
Inbound retention: the inbox is a window, not an archive.

`InboundMessage` rows older than `MESSAGING_RETENTION_DAYS` (default 90) are
purged by the sweep — beat task plus management command, the usual split.
Outbound rows stay: they are the record of what the platform said.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import InboundMessage


def retention_days() -> int:
    try:
        return int(getattr(settings, 'MESSAGING_RETENTION_DAYS', 90))
    except (TypeError, ValueError):
        return 90


def purge_expired(now=None) -> int:
    """Delete inbound rows past retention. Returns the count."""
    before = (now or timezone.now()) - timedelta(days=retention_days())
    deleted, _ = InboundMessage.objects.filter(received_at__lt=before).delete()
    return deleted
