"""
Messages an agent sends to other people: one floor for every channel.

Email (Gmail) and the chat channels (Slack, WhatsApp, Teams, SMS, Telegram)
each had their own per-run and per-recipient caps, but nothing looked across
runs or across channels. An agent on a schedule that fires every five minutes
never trips a per-run cap and can still send a few thousand messages a day
from the owner's own accounts — which is what anti-spam law (TRAI's
commercial-communication rules in India, CAN-SPAM in the US) and every
provider's terms treat as spam, and what gets an account banned. Three checks,
run by the send tools after the user's approval and before the provider call:

* **Content** — the platform's content floor (`content_policy.check_text`)
  applies to what leaves as much as to what comes in.
* **A daily cap per account** across all send tools (`OUTBOUND_DAILY_CAP`,
  counted from the run record, so it survives restarts and spans channels).
* **Disclosure** — a message no person reviewed says it was sent by an AI
  assistant (`OUTBOUND_AI_DISCLOSURE`: `unattended` by default, `always`, or
  `never`). EU AI Act Art. 50 requires people to be told when they are dealing
  with an AI; a message the owner approved on a card is the owner's message,
  one written and sent by a schedule at 3 a.m. is not.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.conf import settings

#: Every tool that delivers a message to another person.
SEND_TOOLS = ('message_send', 'gmail_send_message')

DEFAULT_DAILY_CAP = 100

DISCLOSURE = 'Sent by an AI assistant on behalf of the account owner.'


def _daily_cap() -> int:
    try:
        return max(0, int(getattr(settings, 'OUTBOUND_DAILY_CAP', DEFAULT_DAILY_CAP)))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_CAP


def sent_last_24h(user_id: Any) -> int:
    """Messages this account's agents delivered in the last 24 hours."""
    from django.utils import timezone

    from logs.models import AgentStep

    return AgentStep.objects.filter(
        execution__user_id=user_id, tool__in=SEND_TOOLS, status='completed',
        started_at__gte=timezone.now() - timedelta(hours=24),
    ).count()


def check(user_id: Any, body: str) -> str | None:
    """Why this message may not be sent, or None. Sync: call via sync_to_async."""
    from core.safety.content_policy import check_text

    refused = check_text(body or '', where='outbound message')
    if refused is not None:
        return f'{refused.message} The message was not sent.'
    cap = _daily_cap()
    if cap and user_id and sent_last_24h(user_id) >= cap:
        return (f'This account has sent {cap} messages in the last 24 hours, the '
                f'daily limit. The message was not sent; tell the user, and do not '
                f'retry on another channel.')
    return None


def disclose(body: str, context: dict | None) -> str:
    """`body` with the AI disclosure line when policy asks for one."""
    mode = str(getattr(settings, 'OUTBOUND_AI_DISCLOSURE', 'unattended')).lower()
    if mode == 'never' or DISCLOSURE in (body or ''):
        return body
    if mode != 'always':
        from agents.grants import UNATTENDED_CALLERS

        if (context or {}).get('caller') not in UNATTENDED_CALLERS:
            return body
    return f'{(body or "").rstrip()}\n\n— {DISCLOSURE}'
