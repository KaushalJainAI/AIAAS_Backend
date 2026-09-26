"""
One tool set for every messaging channel, not one set per channel.

`message_channels` lists what the user can address; `message_search` and
`message_read` look back; `message_draft` saves a draft in our DB (nothing
leaves the platform); `message_send` sends new text or a draft id, and is the
only one that is `irreversible` + `sensitive` — recipient and body are
rendered by `describe_call` on the approval card.

Guardrails, because a loop must not be able to spam a customer:
`MAX_MESSAGES_PER_RUN` (20) and 5 per recipient per run, counted on the turn
context; and an unattended run may only send to `recipients` — the
allowlist in `agent_context`. Chat and attended runs fall back to approval.
"""
from __future__ import annotations

import json
import logging
from typing import Dict

from asgiref.sync import sync_to_async

from .registry import tool

from tools_config.overlay import alimit

from tools_config.settings_schema import (
    _TALK_BODY_CHARS,
    _TALK_MAX_PER_RECIPIENT,
    _TALK_MAX_PER_RUN,
    _TALK_READ_LIMIT,
    _TALK_SEARCH_LIMIT,
)

logger = logging.getLogger(__name__)

#: Sends one run may make. A run that has something to say has a few things
#: to say; without a cap, a loop turns a channel into a log. Now workspace
#: knobs (`message_send.maxPerRun/maxPerRecipient`); the constants stay as
#: the floor under a failed overlay read.
MAX_MESSAGES_PER_RUN = 20
#: Per recipient within one run.
MAX_MESSAGES_PER_RECIPIENT = 5
#: Search hits per call. Capped rather than paged: a person picks from these,
#: and a second pagination scheme on a picker is worse than a cap. Now a
#: workspace knob (`message_search.maxResults`).
SEARCH_LIMIT = 50
#: Now a workspace knob (`message_read.maxLimit`).
READ_LIMIT = 100
#: Now a workspace knob (`message_send.bodyChars` / `message_draft.bodyChars`).
BODY_CHARS = 4000


def _channel(args: Dict) -> str:
    from .messaging.common import validate_channel

    try:
        return validate_channel(args.get('channel'))
    except ValueError as exc:
        raise _Refuse(str(exc)) from exc


class _Refuse(ValueError):
    """A use error: rendered as the tool's answer, not a traceback."""


async def _account(user_id: int, channel: str):
    from messaging.models import MessagingAccount

    return await MessagingAccount.objects.filter(
        user_id=user_id, channel=channel,
    ).order_by('-verified', '-updated_at').afirst()


def _sent_counts(context: Dict) -> dict:
    return context.setdefault('_messages_sent', {})


def _check_rate(context: Dict, to: str, max_per_run: int,
                max_per_recipient: int) -> str | None:
    counts = _sent_counts(context)
    total = sum(counts.values())
    if total >= max_per_run:
        return (
            f'This run has already sent {max_per_run} messages, '
            'which is the limit. Put the rest in your answer.')
    if counts.get(to, 0) >= max_per_recipient:
        return (f'{to} already got {max_per_recipient} messages this run.')
    return None


def _recipients(context: Dict) -> list[str] | None:
    raw = context.get('recipients')
    if raw is None:
        return None
    return [str(r).strip() for r in raw if str(r).strip()]


def _recipient_allowed(to: str, allowed: list[str]) -> bool:
    """Exact match, or an `@domain` entry matching that email domain. An
    allowlist that needs globbing to be useful is one nobody can audit."""
    if to in allowed:
        return True
    lower = to.lower()
    return any(
        entry.startswith('@') and lower.endswith(entry.lower())
        for entry in allowed
    )


def _unattended(context: Dict) -> bool:
    from agents.grants import UNATTENDED_CALLERS

    return context.get('caller') in UNATTENDED_CALLERS


@tool({
    'type': 'function',
    'function': {
        'name': 'message_channels',
        'description': (
            'List the messaging channels and addressable targets: Slack '
            'channels and DMs, WhatsApp contacts who wrote to the business, '
            'Teams chats, SMS numbers, Telegram chats. Call it before '
            'sending anywhere you have not sent before.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'channel': {'type': 'string',
                            'description': 'slack, whatsapp, teams, sms or telegram. Omit for all.'},
            },
            'additionalProperties': False,
        },
    },
}, effect='read')
async def message_channels(args: Dict, context: Dict) -> str:
    from .messaging.common import CHANNELS, Unsupported, adapter_for, validate_channel

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    wanted = []
    raw = str(args.get('channel') or '').strip().lower()
    if raw:
        try:
            wanted = [validate_channel(raw)]
        except ValueError as exc:
            return json.dumps({'error': str(exc)})
    else:
        wanted = list(CHANNELS)
    out = []
    for channel in wanted:
        try:
            account = await _account(user_id, channel)
            targets = await adapter_for(channel).list_targets(user_id, account)
            out.append({'channel': channel, 'targets': targets})
        except Unsupported as exc:
            out.append({'channel': channel, 'unavailable': str(exc)})
        except Exception:
            logger.exception('[Talk] message_channels failed')
            out.append({'channel': channel, 'unavailable': 'That channel could not be read.'})
    return json.dumps({'channels': out}, default=str)


@tool({
    'type': 'function',
    'function': {
        'name': 'message_search',
        'description': (
            'Search messaging history: what customers and teammates wrote, '
            'and what was sent back. WhatsApp and SMS search our own inbox '
            'and outbox — the providers keep no history worth using.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'channel': {'type': 'string', 'description': 'slack, whatsapp, teams, sms or telegram.'},
                'query': {'type': 'string', 'description': 'Words to match.'},
                'from': {'type': 'string', 'description': 'Only messages from this sender.'},
                'since': {'type': 'string', 'description': 'Only messages after this date.'},
            },
            'required': ['channel'],
            'additionalProperties': False,
        },
    },
}, effect='read')
async def message_search(args: Dict, context: Dict) -> str:
    from .messaging.common import Unsupported, adapter_for

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        channel = _channel(args)
    except _Refuse as exc:
        return json.dumps({'error': str(exc)})
    try:
        account = await _account(user_id, channel)
        hits = await adapter_for(channel).search(
            user_id, account, query=str(args.get('query') or ''),
            sender=str(args.get('from') or ''), since=str(args.get('since') or ''))
        limited, truncated = _truncate_hits(
            hits, await alimit(context, 'message_search', 'maxResults'))
        return json.dumps({'channel': channel, 'messages': limited,
                           **({'truncated': True} if truncated else {})}, default=str)
    except Unsupported as exc:
        return json.dumps({'channel': channel, 'unavailable': str(exc)})
    except Exception:
        logger.exception('[Talk] message_search failed')
        return json.dumps({'error': 'The search failed.'})


def _truncate_hits(hits: list, cap: int) -> tuple[list, bool]:
    if len(hits) > cap:
        return hits[:cap], True
    return hits, False


@tool({
    'type': 'function',
    'function': {
        'name': 'message_read',
        'description': 'Read one conversation or thread by id, newest last.',
        'parameters': {
            'type': 'object',
            'properties': {
                'channel': {'type': 'string', 'description': 'slack, whatsapp, teams, sms or telegram.'},
                'conversation': {'type': 'string', 'description': 'Channel, thread or contact id.'},
                'limit': {'type': 'integer', 'description': 'Messages to read (default 30, max 100).'},
            },
            'required': ['channel', 'conversation'],
            'additionalProperties': False,
        },
    },
}, effect='read')
async def message_read(args: Dict, context: Dict) -> str:
    from .messaging.common import Unsupported, adapter_for

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        channel = _channel(args)
    except _Refuse as exc:
        return json.dumps({'error': str(exc)})
    conversation = str(args.get('conversation') or '').strip()
    if not conversation:
        return json.dumps({'error': 'Give the conversation or contact id.'})
    read_cap = await alimit(context, 'message_read', 'maxLimit')
    try:
        limit = max(1, min(int(args.get('limit') or 30), read_cap))
    except (TypeError, ValueError):
        return json.dumps({'error': '`limit` must be a number.'})
    try:
        account = await _account(user_id, channel)
        messages = await adapter_for(channel).read(
            user_id, account, conversation=conversation, limit=limit)
        return json.dumps({'channel': channel, 'conversation': conversation,
                           'messages': messages}, default=str)
    except Unsupported as exc:
        return json.dumps({'channel': channel, 'unavailable': str(exc)})
    except Exception:
        logger.exception('[Talk] message_read failed')
        return json.dumps({'error': 'The conversation could not be read.'})


@tool({
    'type': 'function',
    'function': {
        'name': 'message_draft',
        'description': (
            'Draft a message without sending it. The draft lives in the '
            'platform, shown as a card the user can approve — nothing leaves '
            'until `message_send` names its id.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'channel': {'type': 'string', 'description': 'slack, whatsapp, teams, sms or telegram.'},
                'to': {'type': 'string', 'description': 'Who it is for.'},
                'body': {'type': 'string', 'description': 'What it says.'},
            },
            'required': ['channel', 'to', 'body'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def message_draft(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        channel = _channel(args)
    except _Refuse as exc:
        return json.dumps({'error': str(exc)})
    to = str(args.get('to') or '').strip()
    body = str(args.get('body') or '').strip()
    if not to or not body:
        return json.dumps({'error': 'Both `to` and `body` are required.'})
    body_cap = await alimit(context, 'message_draft', 'bodyChars')
    if len(body) > body_cap:
        return json.dumps({
            'error': f'That is {len(body):,} characters; one message holds '
                     f'{body_cap:,}. Split it.'})

    def _save():
        from messaging.models import MessagingAccount, OutboundMessage

        account = MessagingAccount.objects.filter(
            user_id=user_id, channel=channel,
        ).order_by('-verified', '-updated_at').first()
        return OutboundMessage.objects.create(
            user_id=user_id, account=account, channel=channel, to=to,
            body=body, status='draft')

    try:
        row = await sync_to_async(_save)()
    except Exception:
        logger.exception('[Talk] message_draft failed')
        return json.dumps({'error': 'The draft could not be saved.'})
    return json.dumps({
        'draft_id': row.id, 'channel': channel, 'to': to,
        'rendered': f'Drafted for {to} on {channel}. Send it with message_send.',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'message_send',
        'description': (
            'Send a message on a channel — new text, or a draft id from '
            'message_draft. The user approves each send. Unattended runs may '
            'only reach recipients on their allowlist. WhatsApp free text '
            'outside the 24 h window is refused (send `template:<name>`); '
            'SMS needs DLT registration; Teams needs admin consent.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'channel': {'type': 'string', 'description': 'slack, whatsapp, teams, sms or telegram.'},
                'to': {'type': 'string', 'description': 'Who it is for.'},
                'body': {'type': 'string', 'description': 'What it says. Omit when sending a draft.'},
                'draft_id': {'type': 'integer', 'description': 'A draft to send instead of new text.'},
            },
            'required': ['channel', 'to'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='irreversible')
async def message_send(args: Dict, context: Dict) -> str:
    from .messaging.common import Unsupported, adapter_for

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        channel = _channel(args)
    except _Refuse as exc:
        return json.dumps({'error': str(exc)})
    to = str(args.get('to') or '').strip()
    if not to:
        return json.dumps({'error': '`to` is required.'})

    body = str(args.get('body') or '')
    draft = None
    if args.get('draft_id') is not None:
        try:
            draft_id = int(args.get('draft_id'))
        except (TypeError, ValueError):
            return json.dumps({'error': '`draft_id` must be numeric.'})
        from messaging.models import OutboundMessage

        draft = await OutboundMessage.objects.filter(
            id=draft_id, user_id=user_id, channel=channel).afirst()
        if draft is None:
            return json.dumps({'error': f'No draft {draft_id} on {channel} belongs to this user.'})
        if not body:
            body, to = draft.body, draft.to
    body = body.strip()
    if not body:
        return json.dumps({'error': 'There is nothing to send: give `body` or a `draft_id`.'})
    body_cap = await alimit(context, 'message_send', 'bodyChars')
    if len(body) > body_cap:
        return json.dumps({
            'error': f'That is {len(body):,} characters; one message holds '
                     f'{body_cap:,}. Split it.'})

    if _unattended(context):
        allowed = _recipients(context)
        if not allowed:
            return json.dumps({
                'error': 'Unattended runs may only message recipients on their '
                         'allowlist, and this run has none. Add recipients in '
                         'its settings.'})
        if not _recipient_allowed(to, allowed):
            return json.dumps({
                'error': f'{to} is not on this run\'s recipient allowlist.'})
    refused = _check_rate(
        context, to,
        await alimit(context, 'message_send', 'maxPerRun'),
        await alimit(context, 'message_send', 'maxPerRecipient'))
    if refused:
        return json.dumps({'error': refused})
    # One floor across every channel and every run: content policy, a daily
    # cap per account, and an AI disclosure on unreviewed messages.
    from core.safety import outbound

    refused = await sync_to_async(outbound.check)(user_id, body)
    if refused:
        return json.dumps({'error': refused})
    body = outbound.disclose(body, context)

    try:
        account = await _account(user_id, channel)
        provider_id = await adapter_for(channel).send(
            user_id, account, to=to, body=body)
    except Unsupported as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Talk] message_send failed')
        return json.dumps({'error': 'The message could not be sent.'})

    def _save():
        from django.contrib.auth import get_user_model

        from messaging.models import OutboundMessage

        user = get_user_model().objects.filter(id=user_id).first()
        if draft is not None:
            draft.status = 'sent'
            draft.provider_message_id = provider_id
            draft.body = body
            draft.to = to
            draft.save(update_fields=['status', 'provider_message_id', 'body', 'to',
                                      'updated_at'])
            return draft
        return OutboundMessage.objects.create(
            user=user, account=account, channel=channel, to=to, body=body,
            status='sent', provider_message_id=provider_id)

    try:
        row = await sync_to_async(_save)()
    except Exception:
        logger.exception('[Talk] Could not record a sent message')
        return json.dumps({'error': 'The message was sent but could not be recorded.'})
    _sent_counts(context)[to] = _sent_counts(context).get(to, 0) + 1
    await _record_message_cost(context, channel)
    return json.dumps({
        'sent': True, 'channel': channel, 'to': to,
        'provider_message_id': provider_id,
        'rendered': f'Sent to {to} on {channel}.',
    })


async def _record_message_cost(context: Dict, channel: str) -> None:
    """WhatsApp and SMS cost per message; Slack and Teams do not at our scale.
    Estimated, like every non-token spend."""
    if channel not in ('whatsapp', 'sms'):
        return
    from django.contrib.auth import get_user_model

    from logs.costs import record

    user_id = context.get('user_id')
    if not user_id:
        return
    try:
        user = await get_user_model().objects.filter(id=user_id).afirst()
        if user is None:
            return
        from asgiref.sync import sync_to_async as _sta

        await _sta(record)(
            user=user, kind=channel, amount_inr=1, units=1, unit='message',
            estimated=True, source=f"message_send:{context.get('call_id') or ''}",
        )
    except Exception:  # noqa: BLE001
        logger.exception('[Talk] Failed to record message cost')


TALK_TOOLS = ('message_channels', 'message_search', 'message_read',
              'message_draft', 'message_send')
