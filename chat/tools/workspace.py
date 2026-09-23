"""
Tools that reach the platform's own machinery on the user's behalf:
`extract_data` (the extraction engine), `notify_user` (the notification feed,
right now) and the reminder trio (`schedule_notification`,
`list_scheduled_notifications`, `cancel_scheduled_notification` — the feed at
a user-asked time, or on a user-asked heartbeat, fired by the scheduled
sweep in `notifications/scheduled.py`).
"""
from __future__ import annotations

import json
import logging
from typing import Dict

from asgiref.sync import sync_to_async

from .registry import tool

from tools_config.overlay import alimit

from tools_config.settings_schema import (
    _EXTRACT_MAX_DOCS,
    _NOTIFY_MAX_PER_RUN,
)

logger = logging.getLogger(__name__)

#: Notifications one run may send. A run that has something to say has one
#: thing to say; without a cap, a loop turns the feed into a log. Now a
#: per-workspace knob (`notify_user.maxPerRun`); the constant stays as the
#: floor under a failed overlay read.
MAX_NOTIFICATIONS_PER_RUN = 3
TITLE_CHARS = 120
MESSAGE_CHARS = 1000
#: Now a per-workspace knob (`extract_data.maxDocs`); the constant stays as
#: the floor under a failed overlay read.
MAX_EXTRACT_DOCUMENTS = 25


# ---------------------------------------------------------------------------
# extract_data
# ---------------------------------------------------------------------------

def _run_extraction(user_id: int, schema_id: int, paths: list[str], scope) -> dict:
    from inference import vfs
    from inference.extraction import run_extraction
    from inference.models import ExtractionSchema

    schema = ExtractionSchema.objects.filter(id=schema_id, user_id=user_id).first()
    if schema is None:
        available = list(
            ExtractionSchema.objects.filter(user_id=user_id).values('id', 'name')[:20]
        )
        raise ValueError(
            f'No extraction schema {schema_id} belongs to this user. '
            f'Schemas available: {available or "none — create one on the Extraction page."}'
        )

    document_ids = []
    for path in paths:
        parent_parts, leaf = vfs._split_leaf(scope, path)
        folder = vfs._folder_at(scope, parent_parts)
        doc = vfs._document_in(scope, folder, leaf)
        if doc is None:
            raise ValueError(f'No such file: {vfs.render(scope, parent_parts + [leaf])}.')
        document_ids.append(doc.id)

    stats = run_extraction(document_ids, schema.id, user_id)
    return {'schema': schema.name, 'documents': len(document_ids), **stats}


@tool({
    'type': 'function',
    'function': {
        'name': 'extract_data',
        'description': (
            "Pull structured fields out of the user's documents using one of "
            'their saved extraction schemas — invoices, CVs, contracts, forms. '
            'The schema decides the fields and the rows land in the Extraction '
            'page, where low-confidence values wait for review, so this is the '
            'right tool when the same shape is wanted from many files. For a '
            'one-off question about one document, read it instead. Call it with '
            'the schema id the user names; if you do not know it, the error '
            'lists the schemas they have.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'schema_id': {'type': 'integer', 'description': 'The saved schema to apply.'},
                'paths': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': "Documents to extract from.",
                },
            },
            'required': ['schema_id', 'paths'],
            'additionalProperties': False,
        },
    },
}, requires='files', sensitive=True, effect='reversible')
async def extract_data(args: Dict, context: Dict) -> str:
    scope = context.get('file_scope')
    user_id = context.get('user_id')
    if scope is None or not user_id:
        return json.dumps({'error': 'This agent has no file access, so it cannot read documents.'})

    paths = [str(p).strip() for p in (args.get('paths') or []) if str(p).strip()]
    if not paths:
        return json.dumps({'error': 'Give the paths of the documents to extract from.'})
    cap = await alimit(context, "extract_data", "maxDocs")
    if len(paths) > cap:
        return json.dumps({
            'error': f'{len(paths)} documents is more than one call may take '
                     f'({cap}). Run it in batches.'
        })
    try:
        schema_id = int(args.get('schema_id'))
    except (TypeError, ValueError):
        return json.dumps({'error': 'schema_id must be the numeric id of a saved schema.'})

    try:
        out = await sync_to_async(_run_extraction)(user_id, schema_id, paths, scope)
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Extract] extraction failed')
        return json.dumps({'error': 'The extraction failed. Check the schema and try fewer documents.'})

    out['rendered'] = (
        f'Extracted {out.get("created", 0)} row(s) with "{out["schema"]}"; '
        f'{out.get("needs_review", 0)} need review on the Extraction page.'
    )
    return json.dumps(out, default=str)


# ---------------------------------------------------------------------------
# notify_user
# ---------------------------------------------------------------------------

@tool({
    'type': 'function',
    'function': {
        'name': 'notify_user',
        'description': (
            "Send the user a notification in this platform — for when you are "
            'running unattended and something needs them: a long job finished, a '
            'watch found what it was watching for, a step needs a decision you '
            'cannot make. Not for progress updates, and never for something the '
            'user will read in your answer anyway: in a conversation they are '
            'already here. One notification per finding, a few per run at most.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': 'One line, what happened.'},
                'message': {'type': 'string', 'description': 'A sentence or two: what and what next.'},
                'link': {
                    'type': 'string',
                    'description': 'Optional in-app path to open, e.g. /documents or /runs.',
                },
            },
            'required': ['title', 'message'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def notify_user(args: Dict, context: Dict) -> str:
    from django.contrib.auth import get_user_model

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})

    title = str(args.get('title') or '').strip()[:TITLE_CHARS]
    message = str(args.get('message') or '').strip()[:MESSAGE_CHARS]
    if not title or not message:
        return json.dumps({'error': 'Both title and message are required.'})

    # Counted on the turn context, which is per run: a loop cannot fill the
    # feed, and the model is told it has stopped rather than silently dropped.
    # The ceiling is a workspace knob (`notify_user.maxPerRun`).
    cap = await alimit(context, "notify_user", "maxPerRun")
    sent = context.setdefault('_notifications_sent', [0])
    if sent[0] >= cap:
        return json.dumps({
            'error': f'This run has already sent {cap} notifications, '
                     f'which is the limit. Put the rest in your answer.'
        })

    link = str(args.get('link') or '').strip()
    # An in-app path only: a stored value that becomes a link is exactly the
    # shape `lib/nextPath.ts` documents as an open-redirect primitive.
    data = {'action_url': link} if link.startswith('/') and not link.startswith('//') else {}

    def work():
        from notifications.utils import create_notification

        user = get_user_model().objects.filter(id=user_id).first()
        if user is None:
            return None
        notif = create_notification(user, 'agent_update', title, message, data=data, send_email=False)
        if notif is not None:
            # Closed-browser twin of the socket ping above, gated on the same
            # device toggle — never email, never loud when the user muted us.
            try:
                from notifications.models import NotificationPreference
                from notifications.webpush import send_web_push

                prefs = NotificationPreference.objects.filter(user=user).first()
                if prefs is None or prefs.device_notifications_enabled:
                    send_web_push(user, title=title, body=message,
                                  action_url=data.get('action_url') or '/runs',
                                  kind='agent_update')
            except Exception:
                logger.exception('[Notify] web push failed')
        return notif

    try:
        notification = await sync_to_async(work)()
    except Exception:
        logger.exception('[Notify] could not create a notification')
        return json.dumps({'error': 'The notification could not be sent.'})
    if notification is None:
        return json.dumps({'error': 'The notification could not be sent.'})

    sent[0] += 1
    return json.dumps({
        'sent': True,
        'remaining': (await alimit(context, "notify_user", "maxPerRun")) - sent[0],
        'rendered': 'The user has been notified. Do not repeat this in every turn.',
    })


# ---------------------------------------------------------------------------
# schedule_notification / list_scheduled_notifications /
# cancel_scheduled_notification
# ---------------------------------------------------------------------------

@tool({
    'type': 'function',
    'function': {
        'name': 'schedule_notification',
        'description': (
            'Set a reminder the user asked for: at an exact time ("remind me '
            'tomorrow at 9am"), or on a heartbeat they named ("every morning", '
            '"hourly while the migration runs"). The sweep fires it as a '
            'feed notification plus the usual device ping — never email. '
            'Only schedule what the user explicitly asked for, at the exact '
            'time they gave: never invent reminders, never round their time '
            'to something tidier, and never schedule what belongs in your '
            'answer. When a job finishes, call notify_user now instead of '
            'scheduling an echo of it. A time without a timezone is read in '
            'the user\u2019s own timezone.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': 'One line, what the reminder is about.'},
                'message': {'type': 'string', 'description': 'A sentence or two: what and what next.'},
                'run_at': {'type': 'string', 'description': 'ISO date-time, e.g. 2026-09-24T09:00:00+05:30. Must be in the future.'},
                'repeat': {'type': 'string', 'description': 'none (once, the default), hourly, daily or weekly.'},
                'email': {
                    'type': 'boolean',
                    'description': 'Also email it. Pass true only when the user explicitly asked for email — a reminder is feed + ping unless they said the word.',
                },
                'link': {
                    'type': 'string',
                    'description': 'Optional in-app path to open, e.g. /runs.',
                },
            },
            'required': ['title', 'message', 'run_at'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def schedule_notification(args: Dict, context: Dict) -> str:
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    from notifications.scheduled import (
        parse_run_at, schedule_for_user, validate_schedule,
    )

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})

    title = str(args.get('title') or '').strip()[:TITLE_CHARS]
    message = str(args.get('message') or '').strip()[:MESSAGE_CHARS]
    if not title or not message:
        return json.dumps({'error': 'Title, message and run_at are all required.'})
    repeat = str(args.get('repeat') or 'none').lower()

    link = str(args.get('link') or '').strip()
    # Same rule as notify_user: a stored value that becomes a link must be an
    # in-app path, never an open redirect.
    data = {'action_url': link} if link.startswith('/') and not link.startswith('//') else {}

    def work():
        user = get_user_model().objects.filter(id=user_id).first()
        if user is None:
            return None
        now = timezone.now()
        run_at = parse_run_at(str(args.get('run_at') or ''), user)
        validate_schedule(run_at, repeat, now)
        return schedule_for_user(user, title, message, run_at, repeat, data,
                                 send_email=args.get('email') is True)

    try:
        reminder = await sync_to_async(work)()
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Notify] could not schedule a notification')
        return json.dumps({'error': 'The reminder could not be scheduled.'})
    if reminder is None:
        return json.dumps({'error': 'The reminder could not be scheduled.'})
    return json.dumps({
        'scheduled': True,
        'id': reminder.id,
        'run_at': reminder.next_run_at.isoformat(),
        'repeat': reminder.repeat,
        'email': reminder.send_email,
        'rendered': f'Reminder set for {reminder.next_run_at.isoformat()}.',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'list_scheduled_notifications',
        'description': (
            'List this user\u2019s live reminders — what is scheduled, when '
            'each fires next, and whether it repeats. Use it when the user '
            'asks what reminders exist, before scheduling a duplicate, and to '
            'resolve "cancel my morning reminder" to an id.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {},
            'additionalProperties': False,
        },
    },
}, parallel=True, effect='read')
async def list_scheduled_notifications(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})

    def _read():
        from notifications.models import ScheduledNotification

        return list(
            ScheduledNotification.objects
            .filter(user_id=user_id, active=True, next_run_at__isnull=False)
            .order_by('next_run_at')
            .values('id', 'title', 'message', 'repeat', 'send_email',
                    'next_run_at', 'times_sent'))

    try:
        rows = await sync_to_async(_read)()
    except Exception:  # noqa: BLE001
        logger.exception('[Notify] list failed')
        return json.dumps({'error': 'The reminders could not be listed.'})
    return json.dumps({
        'reminders': [
            {**row,
             'next_run_at': row['next_run_at'].isoformat(),
             'message': str(row['message'])[:200]}
            for row in rows
        ],
        'count': len(rows),
    }, default=str)


@tool({
    'type': 'function',
    'function': {
        'name': 'cancel_scheduled_notification',
        'description': (
            'Cancel one of this user\u2019s live reminders by id — resolve '
            'the id with list_scheduled_notifications first, never guess one. '
            'Fired reminders need no cancelling; they already went quiet.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'id': {'type': 'integer', 'description': 'The reminder id.'},
            },
            'required': ['id'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def cancel_scheduled_notification(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        reminder_id = int(args.get('id'))
    except (TypeError, ValueError):
        return json.dumps({'error': 'Give the numeric id of the reminder.'})

    def _cancel():
        from notifications.models import ScheduledNotification

        # Owned only: a foreign id cancels nothing and says it cancelled
        # nothing — the same anti-oracle shape the run tools keep.
        row = (ScheduledNotification.objects
               .filter(id=reminder_id, user_id=user_id,
                       active=True, next_run_at__isnull=False)
               .first())
        if row is None:
            return False
        row.cancel()
        return True

    try:
        cancelled = await sync_to_async(_cancel)()
    except Exception:  # noqa: BLE001
        logger.exception('[Notify] cancel failed')
        return json.dumps({'error': 'The reminder could not be cancelled.'})
    if not cancelled:
        return json.dumps({'error': 'No live reminder with that id for this user.'})
    return json.dumps({'cancelled': True})
