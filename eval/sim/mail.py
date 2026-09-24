"""
Simulated Gmail: the world's mailbox as fixture JSON.

Fixture schema (`fixtures['mail']`):

    {"messages": [
        {"message_id": "m1", "thread_id": "t1",
         "from": "priya@acme.test", "to": "owner@acme.test", "cc": "",
         "subject": "Move the review to Friday",
         "date": "2026-09-20T10:00:00+05:30",
         "labels": ["INBOX", "UNREAD"],
         "body": "Can we move Friday's review to 4pm?",
         "attachments": []}],
     "labels": [{"id": "X", "name": "X", "type": "user"}]   # optional extras
    }

Fixtures are **oldest-first**; searches return newest first, the way the real
`gmail_search_threads` does. Omitted fields default (`labels` to `["INBOX"]`,
`cc`/`body` to `""`, `attachments` to `[]`), so the judge writes the
situation, not the plumbing. Sends go to an **outbox** that starts empty —
nothing leaves the process — and drafts to a drafts list.

Result shapes mirror `chat/tools/google/gmail.py`: thread summaries
(`thread_id, subject, from, date, snippet, message_count, unread`), full
messages (`message_id, thread_id, from, to, cc, subject, date, labels, body,
attachments`), `{"status": "success", …}` for writes, and `"Error: …"` /
`{"error": …, "code": …}` for bad calls.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

TOOLS = (
    'gmail_search_threads',
    'gmail_get_thread',
    'gmail_get_message',
    'gmail_list_labels',
    'gmail_create_draft',
    'gmail_send_message',
    'gmail_modify_labels',
    'gmail_trash_message',
)

SYSTEM_LABELS = [
    {'id': 'INBOX', 'name': 'INBOX', 'type': 'system'},
    {'id': 'UNREAD', 'name': 'UNREAD', 'type': 'system'},
    {'id': 'STARRED', 'name': 'STARRED', 'type': 'system'},
    {'id': 'SENT', 'name': 'SENT', 'type': 'system'},
    {'id': 'DRAFT', 'name': 'DRAFT', 'type': 'system'},
    {'id': 'TRASH', 'name': 'TRASH', 'type': 'system'},
]

#: Message fields a fixture must carry. Checked by `validate_world` so a
#: judge typo fails generation with the field named, not mid-run.
REQUIRED_MESSAGE_KEYS = ('message_id', 'thread_id', 'from', 'to', 'subject', 'body')

_NEWER_THAN = re.compile(r'newer_than:(\d+)d', re.IGNORECASE)
_OLDER_THAN = re.compile(r'older_than:(\d+)d', re.IGNORECASE)


def _parse_date(value: str):
    try:
        return datetime.fromisoformat(str(value or '').replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def _norm_message(raw: dict, index: int) -> dict:
    msg = {
        'message_id': str(raw.get('message_id') or f'm{index}'),
        'thread_id': str(raw.get('thread_id') or f't{index}'),
        'from': str(raw.get('from') or ''),
        'to': str(raw.get('to') or ''),
        'cc': str(raw.get('cc') or ''),
        'subject': str(raw.get('subject') or ''),
        'date': str(raw.get('date') or ''),
        'labels': [str(label) for label in (raw.get('labels') or ['INBOX'])],
        'body': str(raw.get('body') or ''),
        'attachments': list(raw.get('attachments') or []),
        'trashed': False,
    }
    return msg


def _summary(messages: list[dict]) -> dict:
    first, last = messages[0], messages[-1]
    labels = {label for m in messages for label in m['labels']}
    snippet = (last['body'] or '')[:120]
    return {
        'thread_id': first['thread_id'],
        'subject': first['subject'],
        'from': first['from'],
        'date': last['date'],
        'snippet': snippet,
        'message_count': len(messages),
        'unread': 'UNREAD' in labels,
    }


def _full(message: dict) -> dict:
    return {key: message[key] for key in (
        'message_id', 'thread_id', 'from', 'to', 'cc', 'subject',
        'date', 'labels', 'body', 'attachments')}


class MailSim:
    """One attempt's mailbox. Reset per attempt (`prepare`), never shared."""

    TOOLS = TOOLS

    def __init__(self, fixtures: dict | None = None):
        self.reset(fixtures or {})

    def reset(self, fixtures: dict) -> None:
        self.messages = [_norm_message(m, i) for i, m in
                         enumerate(fixtures.get('messages') or [])]
        extra = [dict(label) for label in (fixtures.get('labels') or [])
                 if isinstance(label, dict)]
        self.labels = list(SYSTEM_LABELS) + extra
        self.drafts: list[dict] = []
        self.outbox: list[dict] = []
        self.mutations: list[dict] = []
        self._seq = 0

    # -- dispatch ------------------------------------------------------

    def handles(self, name: str) -> bool:
        return name in TOOLS

    def run(self, name: str, args: dict) -> str:
        try:
            handler = getattr(self, f'_run_{name}', None)
            if handler is None:
                return f"Error: '{name}' is not simulated in this evaluation world."
            return handler(args or {})
        except Exception as exc:  # noqa: BLE001 - a simulator never raises
            return f'Error: {exc}'

    # -- reads ----------------------------------------------------------

    def _visible(self) -> list[dict]:
        return [m for m in self.messages if not m['trashed']]

    def _run_gmail_search_threads(self, args: dict) -> str:
        query = str(args.get('query') or '')
        terms, froms, subjects, unread_only, newer, older = self._parse_query(query)
        hits = []
        for message in self._visible():
            if unread_only and 'UNREAD' not in message['labels']:
                continue
            if froms and not any(f in message['from'].lower() for f in froms):
                continue
            if subjects and not any(s in message['subject'].lower() for s in subjects):
                continue
            when = _parse_date(message['date'])
            if newer is not None and (when is None or when < newer):
                continue
            if older is not None and (when is None or when > older):
                continue
            blob = ' '.join((message['from'], message['to'], message['subject'],
                             message['body'])).lower()
            if terms and not all(t in blob for t in terms):
                continue
            hits.append(message)
        try:
            limit = int(args.get('max_results') or 0) or 20
        except (TypeError, ValueError):
            limit = 20
        threads: dict[str, list[dict]] = {}
        for message in hits:
            threads.setdefault(message['thread_id'], []).append(message)
        # Newest first: fixtures are oldest-first, so later rows win.
        summaries = [_summary(threads[tid]) for tid in threads]
        summaries.reverse()
        summaries = summaries[:max(1, min(limit, 50))]
        return json.dumps({'type': 'gmail_threads', 'query': query.strip(),
                           'count': len(summaries), 'threads': summaries})

    @staticmethod
    def _parse_query(query: str):
        terms, froms, subjects = [], [], []
        unread_only = 'is:unread' in query.lower()
        newer = older = None
        now = datetime.now().astimezone()
        match = _NEWER_THAN.search(query)
        if match:
            newer = now - timedelta(days=int(match.group(1)))
        match = _OLDER_THAN.search(query)
        if match:
            older = now - timedelta(days=int(match.group(1)))
        for token in query.split():
            low = token.lower()
            if low.startswith('from:'):
                froms.append(low[5:])
            elif low.startswith('subject:'):
                subjects.append(low[8:])
            elif ':' in token and any(low.startswith(p) for p in (
                    'is:', 'newer_than:', 'older_than:', 'in:', 'label:')):
                continue
            elif token.strip():
                terms.append(low)
        return terms, froms, subjects, unread_only, newer, older

    def _thread(self, thread_id: str) -> list[dict]:
        return [m for m in self._visible() if m['thread_id'] == thread_id]

    def _run_gmail_get_thread(self, args: dict) -> str:
        thread_id = str(args.get('thread_id') or '').strip()
        if not thread_id:
            return "Error: 'thread_id' is required."
        messages = self._thread(thread_id)
        if not messages:
            return json.dumps({'error': f'Unknown thread {thread_id}.',
                               'code': 'not_found'})
        return json.dumps({'type': 'gmail_thread', 'thread_id': thread_id,
                           'messages': [_full(m) for m in messages],
                           'truncated': False})

    def _message(self, message_id: str) -> dict | None:
        return next((m for m in self._visible()
                     if m['message_id'] == message_id), None)

    def _run_gmail_get_message(self, args: dict) -> str:
        message_id = str(args.get('message_id') or '').strip()
        if not message_id:
            return "Error: 'message_id' is required."
        message = self._message(message_id)
        if message is None:
            return json.dumps({'error': f'Unknown message {message_id}.',
                               'code': 'not_found'})
        return json.dumps({'type': 'gmail_message', **_full(message),
                           'truncated': False})

    def _run_gmail_list_labels(self, args: dict) -> str:
        return json.dumps({'type': 'gmail_labels', 'labels': self.labels})

    # -- writes ----------------------------------------------------------

    def _compose(self, args: dict) -> tuple[dict | None, str | None]:
        to = str(args.get('to') or '').strip()
        if not to:
            return None, 'A recipient (\'to\') is required.'
        if any(c in to for c in '\r\n'):
            return None, 'Addresses may not contain line breaks.'
        subject = str(args.get('subject') or '')
        if any(c in subject for c in '\r\n'):
            return None, 'The subject may not contain line breaks.'
        thread_id = None
        reply_to = str(args.get('reply_to_message_id') or '').strip()
        if reply_to:
            original = next((m for m in self.messages
                             if m['message_id'] == reply_to), None)
            if original is None:
                return None, f'Unknown message {reply_to}.'
            thread_id = original['thread_id']
            if not subject:
                subject = original['subject']
                if not subject.lower().startswith('re:'):
                    subject = f'Re: {subject}'
            if not to:
                to = original['from']
        return {'to': to, 'cc': str(args.get('cc') or ''),
                'bcc': str(args.get('bcc') or ''), 'subject': subject,
                'body': str(args.get('body') or ''), 'thread_id': thread_id}, None

    def _run_gmail_create_draft(self, args: dict) -> str:
        composed, error = self._compose(args)
        if error is not None:
            return json.dumps({'error': error, 'code': 'bad_request'})
        self._seq += 1
        draft = {'draft_id': f'sim-draft-{self._seq}',
                 'message_id': f'sim-msg-{self._seq}', **composed}
        self.drafts.append(draft)
        self.mutations.append({'draft': draft['subject'], 'to': draft['to']})
        return json.dumps({'status': 'success', 'draft_id': draft['draft_id'],
                           'message_id': draft['message_id'],
                           'text': 'Draft saved. It has not been sent.'})

    def _run_gmail_send_message(self, args: dict) -> str:
        composed, error = self._compose(args)
        if error is not None:
            return json.dumps({'error': error, 'code': 'bad_request'})
        self._seq += 1
        sent = {'message_id': f'sim-msg-{self._seq}', **composed}
        self.outbox.append(sent)
        self.mutations.append({'sent': sent['subject'], 'to': sent['to']})
        return json.dumps({'status': 'success', 'message_id': sent['message_id'],
                           'thread_id': sent['thread_id'],
                           'text': 'Email sent.'})

    def _resolve_labels(self, names) -> tuple[list[str], str | None]:
        by_name = {(label.get('name') or '').lower(): label.get('id')
                   for label in self.labels}
        ids = {label.get('id') for label in self.labels}
        resolved = []
        for name in names or []:
            name = str(name)
            if name in ids:
                resolved.append(name)
            elif name.lower() in by_name:
                resolved.append(by_name[name.lower()])
            else:
                return [], f'No Gmail label named {name!r}.'
        return resolved, None

    def _run_gmail_modify_labels(self, args: dict) -> str:
        message_id = str(args.get('message_id') or '').strip()
        thread_id = str(args.get('thread_id') or '').strip()
        if bool(message_id) == bool(thread_id):
            return "Error: give exactly one of 'message_id' or 'thread_id'."
        add, error = self._resolve_labels(args.get('add_labels'))
        if error is not None:
            return json.dumps({'error': error, 'code': 'not_found'})
        remove, error = self._resolve_labels(args.get('remove_labels'))
        if error is not None:
            return json.dumps({'error': error, 'code': 'not_found'})
        if not add and not remove:
            return 'Error: give at least one label to add or remove.'
        if message_id:
            targets = [m for m in self._visible() if m['message_id'] == message_id]
            if not targets:
                return json.dumps({'error': f'Unknown message {message_id}.',
                                   'code': 'not_found'})
        else:
            targets = self._thread(thread_id)
            if not targets:
                return json.dumps({'error': f'Unknown thread {thread_id}.',
                                   'code': 'not_found'})
        for message in targets:
            labels = [label for label in message['labels'] if label not in remove]
            labels += [label for label in add if label not in labels]
            message['labels'] = labels
            self.mutations.append({'labels': message['message_id'],
                                   'added': add, 'removed': remove})
        return json.dumps({'status': 'success', 'added': add, 'removed': remove})

    def _run_gmail_trash_message(self, args: dict) -> str:
        message_id = str(args.get('message_id') or '').strip()
        if not message_id:
            return "Error: 'message_id' is required."
        message = self._message(message_id)
        if message is None:
            return json.dumps({'error': f'Unknown message {message_id}.',
                               'code': 'not_found'})
        message['trashed'] = True
        message['labels'] = [label for label in message['labels']
                             if label not in ('INBOX', 'UNREAD')]
        self.mutations.append({'trashed': message_id})
        return json.dumps({'status': 'success', 'text': 'Moved to Trash.'})

    # -- grading ----------------------------------------------------------

    def snapshot(self) -> dict:
        """Inbox label state plus everything the attempt sent or drafted."""
        return {
            'messages': [{'message_id': m['message_id'],
                          'thread_id': m['thread_id'],
                          'labels': list(m['labels']),
                          'trashed': m['trashed']} for m in self.messages],
            'outbox': [dict(s) for s in self.outbox],
            'drafts': [dict(d) for d in self.drafts],
        }

    def changes(self) -> dict:
        """What the attempt did to the mailbox, capped for the reviewer."""
        out: dict = {}
        if self.outbox:
            out['sent'] = [{'to': s['to'], 'subject': s['subject']}
                           for s in self.outbox[:20]]
        if self.drafts:
            out['drafts'] = [{'to': d['to'], 'subject': d['subject']}
                             for d in self.drafts[:20]]
        labels = [m for m in self.mutations if 'labels' in m][:20]
        if labels:
            out['labels_changed'] = labels
        trashed = [m['trashed'] for m in self.mutations if 'trashed' in m][:20]
        if trashed:
            out['trashed'] = trashed
        return out

    def apply_expected(self, expect: dict) -> None:
        """Perform the case's expected sends/drafts (generation-time proof)."""
        for key, method in (('sent', self._run_gmail_send_message),
                            ('drafts', self._run_gmail_create_draft)):
            for item in (expect or {}).get(key) or []:
                if isinstance(item, dict):
                    method(dict(item))


__all__ = ['TOOLS', 'MailSim', 'SYSTEM_LABELS']
