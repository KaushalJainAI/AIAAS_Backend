"""
Simulated Google Calendar: the world's schedule as fixture JSON.

Fixture schema (`fixtures['calendar']`):

    {"calendars": [{"id": "primary", "name": "Acme", "time_zone": "Asia/Kolkata"}],
     "events": [{"event_id": "e1", "calendar_id": "primary",
                 "title": "Q3 review", "start": "2026-09-25T16:00:00+05:30",
                 "end": "2026-09-25T17:00:00+05:30",
                 "attendees": ["priya@acme.test"], "description": "",
                 "location": ""}]}

Times are opaque strings the fixtures and the agent agree on (RFC 3339 in
practice). Window overlap compares ISO-looking strings directly and lets
anything else through — a simulator that refused an unusual format would be
stricter than the service it stands in for.

Result shapes mirror `chat/tools/google/calendar.py`: calendar lists
(`calendar_id, name, primary, time_zone, access`), event summaries
(`event_id, summary, start, end, all_day, location, description, status,
organizer, attendees[{email, response, self}], meet_link, link`),
`{"status": "success", …}` for writes, and `"Error: …"` / `{"error": …}`
for bad calls.
"""
from __future__ import annotations

import copy
import json

TOOLS = (
    'calendar_list_calendars',
    'calendar_list_events',
    'calendar_get_event',
    'calendar_find_free_time',
    'calendar_create_event',
    'calendar_update_event',
    'calendar_respond_to_event',
    'calendar_delete_event',
)

#: Event fields a fixture must carry. Checked by `validate_world`.
REQUIRED_EVENT_KEYS = ('event_id', 'title', 'start')


def _norm_attendee(raw) -> dict:
    if isinstance(raw, dict):
        return {'email': str(raw.get('email') or ''),
                'response': str(raw.get('response') or 'needsAction'),
                'self': bool(raw.get('self'))}
    return {'email': str(raw or ''), 'response': 'needsAction', 'self': False}


def _norm_event(raw: dict, index: int) -> dict:
    return {
        'event_id': str(raw.get('event_id') or f'e{index}'),
        'calendar_id': str(raw.get('calendar_id') or 'primary'),
        'summary': str(raw.get('title') or raw.get('summary') or ''),
        'start': str(raw.get('start') or ''),
        'end': str(raw.get('end') or ''),
        'all_day': bool(raw.get('all_day', 'T' not in str(raw.get('start') or ''))),
        'location': str(raw.get('location') or ''),
        'description': str(raw.get('description') or ''),
        'status': str(raw.get('status') or 'confirmed'),
        'organizer': str(raw.get('organizer') or ''),
        'attendees': [_norm_attendee(a) for a in (raw.get('attendees') or [])],
        'meet_link': raw.get('meet_link'),
        'link': raw.get('link'),
    }


def _iso(value: str) -> bool:
    return 'T' in str(value or '')


def _overlaps(start: str, end: str, window_min: str, window_max: str) -> bool:
    """Window overlap for ISO-looking bounds; anything else is let through.

    Half-open like the service: an event ending exactly at the window start
    does not overlap it.
    """
    if not (window_min or window_max):
        return True
    if not (_iso(start) and _iso(end or start)
            and _iso(window_min or start) and _iso(window_max or end or start)):
        return True
    lo = window_min or start
    hi = window_max or end or start
    return not (end <= lo or start >= hi)


class CalendarSim:
    """One attempt's schedule. Reset per attempt, never shared."""

    TOOLS = TOOLS

    def __init__(self, fixtures: dict | None = None):
        self.reset(fixtures or {})

    def reset(self, fixtures: dict) -> None:
        self.calendars = [dict(c) for c in (fixtures.get('calendars') or [])]
        if not any(c.get('id') == 'primary' for c in self.calendars):
            self.calendars = [{'id': 'primary', 'name': 'Primary',
                               'primary': True, 'time_zone': 'UTC',
                               'access': 'owner'}] + self.calendars
        self.events = [_norm_event(e, i) for i, e in
                       enumerate(fixtures.get('events') or [])]
        self.deleted: list[str] = []
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

    def _run_calendar_list_calendars(self, args: dict) -> str:
        return json.dumps({'type': 'calendars', 'calendars': self.calendars})

    def _in_calendar(self, event: dict, calendar_id: str) -> bool:
        return event['calendar_id'] == (calendar_id or 'primary')

    def _run_calendar_list_events(self, args: dict) -> str:
        calendar_id = str(args.get('calendar_id') or 'primary')
        query = str(args.get('query') or '').lower()
        time_min = str(args.get('time_min') or '')
        time_max = str(args.get('time_max') or '')
        hits = []
        for event in self.events:
            if not self._in_calendar(event, calendar_id):
                continue
            if query and query not in ' '.join((
                    event['summary'], event['description'],
                    event['location'],
                    *(a['email'] for a in event['attendees']))).lower():
                continue
            if not _overlaps(event['start'], event['end'] or event['start'],
                             time_min, time_max):
                continue
            hits.append(event)
        hits.sort(key=lambda e: e['start'])
        try:
            limit = int(args.get('max_results') or 0) or 20
        except (TypeError, ValueError):
            limit = 20
        hits = hits[:max(1, min(limit, 50))]
        return json.dumps({'type': 'calendar_events', 'time_zone': 'UTC',
                           'count': len(hits), 'events': hits})

    def _event(self, event_id: str) -> dict | None:
        return next((e for e in self.events if e['event_id'] == event_id), None)

    def _run_calendar_get_event(self, args: dict) -> str:
        event_id = str(args.get('event_id') or '').strip()
        if not event_id:
            return "Error: 'event_id' is required."
        event = self._event(event_id)
        if event is None:
            return json.dumps({'error': f'Unknown event {event_id}.',
                               'code': 'not_found'})
        return json.dumps({'type': 'calendar_event', **copy.deepcopy(event)})

    def _run_calendar_find_free_time(self, args: dict) -> str:
        ids = [str(i) for i in args.get('calendar_ids') or [] if str(i).strip()]
        ids = ids[:20] or ['primary']
        time_min = str(args.get('time_min') or '')
        time_max = str(args.get('time_max') or '')
        out = {}
        for cid in ids:
            busy = [{'start': e['start'], 'end': e['end'] or e['start']}
                    for e in self.events
                    if self._in_calendar(e, cid)
                    and _overlaps(e['start'], e['end'] or e['start'],
                                  time_min, time_max)]
            out[cid] = {'busy': busy, 'errors': []}
        return json.dumps({'type': 'calendar_free_busy', 'calendars': out})

    # -- writes ----------------------------------------------------------

    def _run_calendar_create_event(self, args: dict) -> str:
        summary = str(args.get('summary') or '').strip()
        start = str(args.get('start') or '').strip()
        end = str(args.get('end') or '').strip()
        if not summary:
            return "Error: 'summary' is required."
        if not (start and end):
            return "Error: Both 'start' and 'end' are required."
        self._seq += 1
        event = _norm_event({
            'event_id': f'sim-event-{self._seq}',
            'calendar_id': str(args.get('calendar_id') or 'primary'),
            'title': summary, 'start': start, 'end': end,
            'description': args.get('description') or '',
            'location': args.get('location') or '',
            'attendees': args.get('attendees') or [],
        }, self._seq)
        self.events.append(event)
        self.mutations.append({'created': event['event_id'],
                               'title': summary})
        # `status` reads 'confirmed': the real tool answers
        # `{"status": "success", **summary}`, and the summary's own status
        # overwrites it — mirrored here exactly, quirk included.
        return json.dumps({'status': 'success', **copy.deepcopy(event)})

    def _run_calendar_update_event(self, args: dict) -> str:
        event_id = str(args.get('event_id') or '').strip()
        if not event_id:
            return "Error: 'event_id' is required."
        event = self._event(event_id)
        if event is None:
            return json.dumps({'error': f'Unknown event {event_id}.',
                               'code': 'not_found'})
        changed = False
        for key in ('summary', 'description', 'location'):
            if args.get(key) is not None:
                event[key] = str(args[key])
                changed = True
        for key in ('start', 'end'):
            if args.get(key):
                event[key] = str(args[key])
                changed = True
        if args.get('attendees') is not None:
            event['attendees'] = [_norm_attendee(a) for a in args['attendees']]
            changed = True
        if not changed:
            return 'Error: give at least one field to change.'
        self.mutations.append({'updated': event_id})
        return json.dumps({'status': 'success', **copy.deepcopy(event)})

    def _run_calendar_respond_to_event(self, args: dict) -> str:
        event_id = str(args.get('event_id') or '').strip()
        response = args.get('response')
        if not event_id or response not in ('accepted', 'declined', 'tentative'):
            return ("Error: 'event_id' and a 'response' of accepted, declined "
                    'or tentative are required.')
        event = self._event(event_id)
        if event is None:
            return json.dumps({'error': f'Unknown event {event_id}.',
                               'code': 'not_found'})
        mine = [a for a in event['attendees'] if a.get('self')]
        if not mine:
            return json.dumps({
                'error': 'The user is not an attendee of that event, so there '
                         'is nothing to respond to.',
                'code': 'bad_request'})
        for attendee in mine:
            attendee['response'] = response
        self.mutations.append({'responded': event_id, 'response': response})
        return json.dumps({'status': 'success', 'response': response,
                           **copy.deepcopy(event)})

    def _run_calendar_delete_event(self, args: dict) -> str:
        event_id = str(args.get('event_id') or '').strip()
        if not event_id:
            return "Error: 'event_id' is required."
        event = self._event(event_id)
        if event is None:
            return json.dumps({'error': f'Unknown event {event_id}.',
                               'code': 'not_found'})
        self.events = [e for e in self.events if e['event_id'] != event_id]
        self.deleted.append(event_id)
        self.mutations.append({'deleted': event_id, 'title': event['summary']})
        return json.dumps({'status': 'success', 'text': 'Event deleted.'})

    # -- grading ----------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            'events': [copy.deepcopy(e) for e in self.events],
            'deleted': list(self.deleted),
        }

    def changes(self) -> dict:
        out: dict = {}
        created = [m for m in self.mutations if 'created' in m][:20]
        if created:
            out['events_created'] = [
                {'title': m['title'], 'event_id': m['created']} for m in created]
        updated = [m['updated'] for m in self.mutations if 'updated' in m][:20]
        if updated:
            out['events_updated'] = updated
        responded = [m for m in self.mutations if 'responded' in m][:20]
        if responded:
            out['responses'] = responded
        deleted = [m for m in self.mutations if 'deleted' in m][:20]
        if deleted:
            out['events_deleted'] = [
                {'title': m['title'], 'event_id': m['deleted']} for m in deleted]
        return out

    def apply_expected(self, expect: dict) -> None:
        """Perform the case's expected event changes (generation-time proof)."""
        for item in (expect or {}).get('events') or []:
            if isinstance(item, dict):
                self._run_calendar_create_event(dict(item))


__all__ = ['TOOLS', 'CalendarSim']
