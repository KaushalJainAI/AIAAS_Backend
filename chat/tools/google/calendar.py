"""
Google Calendar, over `calendar/v3`.

Every write here is declared `irreversible` even though an event can be edited
back: creating, moving, cancelling or answering an event **notifies its
attendees**, and an email already delivered to a colleague is not undone by
deleting the event. That is the question `effect` answers — what happens if
nobody was asked — so `auto` autonomy still stops before any of them.

Times are passed through as the model gives them (RFC 3339, or `YYYY-MM-DD` for
an all-day event) rather than parsed here. Google's parser is the one whose
answer counts, and a second one in front of it can only disagree.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict
from urllib.parse import quote

from tools_config.overlay import alimit

from ..registry import tool
from .client import GoogleAPIError, get_json, handles_google_errors, send_json

API = "https://www.googleapis.com/calendar/v3"
CONNECTOR = "google-calendar"

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SEND_UPDATES = {
    "type": "string", "enum": ["all", "externalOnly", "none"],
    "description": "Who Google emails about this change (default 'all').",
}


def _calendar_id(args: Dict) -> str:
    return quote((args.get("calendar_id") or "primary").strip() or "primary", safe="@.")


def _when(value: str, time_zone: str | None) -> dict[str, str]:
    value = (value or "").strip()
    if _DATE_ONLY.match(value):
        return {"date": value}
    when = {"dateTime": value}
    if time_zone:
        when["timeZone"] = time_zone
    return when


def _event_summary(e: dict) -> dict[str, Any]:
    start = e.get("start") or {}
    end = e.get("end") or {}
    return {
        "event_id": e.get("id"),
        "summary": e.get("summary", ""),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "date" in start,
        "location": e.get("location", ""),
        "description": (e.get("description") or "")[:2000],
        "status": e.get("status"),
        "organizer": (e.get("organizer") or {}).get("email"),
        "attendees": [
            {"email": a.get("email"), "response": a.get("responseStatus"), "self": bool(a.get("self"))}
            for a in e.get("attendees") or []
        ],
        "meet_link": e.get("hangoutLink"),
        "link": e.get("htmlLink"),
    }


@tool({
    "type": "function",
    "function": {
        "name": "calendar_list_calendars",
        "description": "[Google Calendar] List the calendars the user can see, with ids and time zones.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}, parallel=True, effect="read", connector=CONNECTOR)
@handles_google_errors
async def calendar_list_calendars(args: Dict, context: Dict) -> str:
    data = await get_json(context, f"{API}/users/me/calendarList")
    calendars = [
        {
            "calendar_id": c.get("id"),
            "name": c.get("summaryOverride") or c.get("summary"),
            "primary": bool(c.get("primary")),
            "time_zone": c.get("timeZone"),
            "access": c.get("accessRole"),
        }
        for c in data.get("items") or []
    ]
    return json.dumps({"type": "calendars", "calendars": calendars})


@tool({
    "type": "function",
    "function": {
        "name": "calendar_list_events",
        "description": (
            "[Google Calendar] List events in a time window, soonest first. Times "
            "are RFC 3339 (e.g. '2026-09-18T00:00:00+05:30'); time_min defaults to "
            "now. 'query' filters by text. Recurring events are expanded."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "description": "Defaults to the primary calendar."},
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector=CONNECTOR)
@handles_google_errors
async def calendar_list_events(args: Dict, context: Dict) -> str:
    cap = await alimit(context, "calendar_list_events", "maxResults")
    requested = args.get("max_results")
    limit = min(int(requested), cap) if isinstance(requested, int) and requested > 0 else cap
    params: dict[str, Any] = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": limit,
        "timeMin": (args.get("time_min") or "").strip()
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if (args.get("time_max") or "").strip():
        params["timeMax"] = args["time_max"].strip()
    if (args.get("query") or "").strip():
        params["q"] = args["query"].strip()
    data = await get_json(context, f"{API}/calendars/{_calendar_id(args)}/events", params=params)
    events = [_event_summary(e) for e in data.get("items") or []]
    return json.dumps({
        "type": "calendar_events",
        "time_zone": data.get("timeZone"),
        "count": len(events),
        "events": events,
    })


@tool({
    "type": "function",
    "function": {
        "name": "calendar_get_event",
        "description": "[Google Calendar] Get one event's full details, including attendees and their responses.",
        "parameters": {
            "type": "object",
            "properties": {"event_id": {"type": "string"}, "calendar_id": {"type": "string"}},
            "required": ["event_id"],
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector=CONNECTOR)
@handles_google_errors
async def calendar_get_event(args: Dict, context: Dict) -> str:
    event_id = (args.get("event_id") or "").strip()
    if not event_id:
        return "Error: 'event_id' is required."
    e = await get_json(context, f"{API}/calendars/{_calendar_id(args)}/events/{quote(event_id)}")
    return json.dumps({"type": "calendar_event", **_event_summary(e)})


@tool({
    "type": "function",
    "function": {
        "name": "calendar_find_free_time",
        "description": (
            "[Google Calendar] Show the busy intervals of one or more calendars "
            "in a window, to find a free slot. Times are RFC 3339."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "time_min": {"type": "string"},
                "time_max": {"type": "string"},
                "calendar_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Calendar ids or attendee emails; defaults to the primary calendar.",
                },
                "time_zone": {"type": "string"},
            },
            "required": ["time_min", "time_max"],
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector=CONNECTOR)
@handles_google_errors
async def calendar_find_free_time(args: Dict, context: Dict) -> str:
    ids = [str(i) for i in args.get("calendar_ids") or [] if str(i).strip()] or ["primary"]
    body: dict[str, Any] = {
        "timeMin": args.get("time_min"),
        "timeMax": args.get("time_max"),
        "items": [{"id": i} for i in ids[:20]],
    }
    if args.get("time_zone"):
        body["timeZone"] = args["time_zone"]
    data = await send_json(context, "POST", f"{API}/freeBusy", json=body)
    calendars = {
        cid: {
            "busy": info.get("busy") or [],
            "errors": [e.get("reason") for e in info.get("errors") or []],
        }
        for cid, info in (data.get("calendars") or {}).items()
    }
    return json.dumps({"type": "calendar_free_busy", "calendars": calendars})


_EVENT_FIELDS = {
    "summary": {"type": "string"},
    "start": {"type": "string", "description": "RFC 3339 date-time, or YYYY-MM-DD for all-day."},
    "end": {"type": "string", "description": "RFC 3339 date-time, or YYYY-MM-DD (exclusive) for all-day."},
    "time_zone": {"type": "string", "description": "IANA zone, e.g. 'Asia/Kolkata'."},
    "description": {"type": "string"},
    "location": {"type": "string"},
    "attendees": {"type": "array", "items": {"type": "string"}, "description": "Attendee emails."},
}


def _event_body(args: Dict, *, partial: bool) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for key in ("summary", "description", "location"):
        if args.get(key) is not None:
            body[key] = args[key]
    tz = args.get("time_zone")
    for key in ("start", "end"):
        if args.get(key):
            body[key] = _when(args[key], tz)
    if args.get("attendees") is not None:
        body["attendees"] = [{"email": a} for a in args["attendees"] if str(a).strip()]
    if not partial and not ("start" in body and "end" in body):
        raise GoogleAPIError("bad_request", "Both 'start' and 'end' are required.")
    return body


@tool({
    "type": "function",
    "function": {
        "name": "calendar_create_event",
        "description": (
            "[Google Calendar] Create an event. Attendees are emailed an "
            "invitation unless send_updates is 'none'. Set add_meet_link to "
            "attach a Google Meet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                **_EVENT_FIELDS,
                "calendar_id": {"type": "string"},
                "add_meet_link": {"type": "boolean"},
                "send_updates": _SEND_UPDATES,
            },
            "required": ["summary", "start", "end"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible", connector=CONNECTOR)
@handles_google_errors
async def calendar_create_event(args: Dict, context: Dict) -> str:
    body = _event_body(args, partial=False)
    params: dict[str, Any] = {"sendUpdates": args.get("send_updates") or "all"}
    if args.get("add_meet_link"):
        import uuid

        body["conferenceData"] = {"createRequest": {
            "requestId": uuid.uuid4().hex,
            "conferenceSolutionKey": {"type": "hangoutsMeet"},
        }}
        params["conferenceDataVersion"] = 1
    e = await send_json(
        context, "POST", f"{API}/calendars/{_calendar_id(args)}/events",
        params=params, json=body,
    )
    return json.dumps({"status": "success", **_event_summary(e)})


@tool({
    "type": "function",
    "function": {
        "name": "calendar_update_event",
        "description": (
            "[Google Calendar] Change an existing event. Only the fields given "
            "are changed; 'attendees' replaces the whole attendee list."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string"},
                **_EVENT_FIELDS,
                "send_updates": _SEND_UPDATES,
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible", connector=CONNECTOR)
@handles_google_errors
async def calendar_update_event(args: Dict, context: Dict) -> str:
    event_id = (args.get("event_id") or "").strip()
    if not event_id:
        return "Error: 'event_id' is required."
    body = _event_body(args, partial=True)
    if not body:
        return "Error: give at least one field to change."
    e = await send_json(
        context, "PATCH", f"{API}/calendars/{_calendar_id(args)}/events/{quote(event_id)}",
        params={"sendUpdates": args.get("send_updates") or "all"}, json=body,
    )
    return json.dumps({"status": "success", **_event_summary(e)})


@tool({
    "type": "function",
    "function": {
        "name": "calendar_respond_to_event",
        "description": "[Google Calendar] Accept, decline or tentatively accept an invitation on the user's behalf.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string"},
                "response": {"type": "string", "enum": ["accepted", "declined", "tentative"]},
            },
            "required": ["event_id", "response"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible", connector=CONNECTOR)
@handles_google_errors
async def calendar_respond_to_event(args: Dict, context: Dict) -> str:
    event_id = (args.get("event_id") or "").strip()
    response = args.get("response")
    if not event_id or response not in ("accepted", "declined", "tentative"):
        return "Error: 'event_id' and a 'response' of accepted, declined or tentative are required."
    url = f"{API}/calendars/{_calendar_id(args)}/events/{quote(event_id)}"
    e = await get_json(context, url)
    attendees = e.get("attendees") or []
    mine = [a for a in attendees if a.get("self")]
    if not mine:
        return json.dumps({
            "error": "The user is not an attendee of that event, so there is nothing to respond to.",
            "code": "bad_request",
        })
    for a in mine:
        a["responseStatus"] = response
    e = await send_json(
        context, "PATCH", url, params={"sendUpdates": "all"}, json={"attendees": attendees},
    )
    return json.dumps({"status": "success", "response": response, **_event_summary(e)})


@tool({
    "type": "function",
    "function": {
        "name": "calendar_delete_event",
        "description": "[Google Calendar] Delete (cancel) an event. Attendees are notified unless send_updates is 'none'.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string"},
                "send_updates": _SEND_UPDATES,
            },
            "required": ["event_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible", connector=CONNECTOR)
@handles_google_errors
async def calendar_delete_event(args: Dict, context: Dict) -> str:
    event_id = (args.get("event_id") or "").strip()
    if not event_id:
        return "Error: 'event_id' is required."
    await send_json(
        context, "DELETE", f"{API}/calendars/{_calendar_id(args)}/events/{quote(event_id)}",
        params={"sendUpdates": args.get("send_updates") or "all"},
    )
    return json.dumps({"status": "success", "text": "Event deleted."})
