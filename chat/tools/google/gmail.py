"""
Gmail, over the Gmail REST API (`gmail/v1`).

Read tools return decoded text, never raw MIME: a model handed base64url
payload parts either spends its window decoding them or, more often, reports
that the email "appears to be encoded". Bodies prefer `text/plain` and fall
back to `text/html` converted to text, because marketing mail often ships HTML
alone and an empty body reads as an empty email.

Mail is third-party text. It is returned as data under a `body` key, the same
way `read_url` returns a page — the rule that it is not instruction lives in
the prompt, and nothing here renders it.
"""
from __future__ import annotations

import asyncio
import base64
import json
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any, Dict

from tools_config.overlay import alimit

from ..registry import tool
from .client import GoogleAPIError, get_json, handles_google_errors, send_json

API = "https://gmail.googleapis.com/gmail/v1/users/me"
CONNECTOR = "gmail"

#: Threads fetched in detail by one search, concurrently. Each is a metadata
#: request, and Gmail's per-user quota is generous but not unlimited.
_DETAIL_CONCURRENCY = 5

_SUMMARY_HEADERS = ("From", "To", "Subject", "Date")


def _headers(payload: dict) -> dict[str, str]:
    return {
        h.get("name", ""): h.get("value", "")
        for h in (payload or {}).get("headers") or []
    }


def _decode(data: str | None) -> str:
    if not data:
        return ""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover — bs4 is a hard requirement
        return html
    return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


def _walk(part: dict, plain: list[str], html: list[str], attachments: list[dict]) -> None:
    mime = part.get("mimeType", "")
    body = part.get("body") or {}
    filename = part.get("filename")
    if filename:
        attachments.append({
            "filename": filename,
            "mime_type": mime,
            "size": body.get("size", 0),
        })
    elif mime == "text/plain":
        plain.append(_decode(body.get("data")))
    elif mime == "text/html":
        html.append(_decode(body.get("data")))
    for child in part.get("parts") or []:
        _walk(child, plain, html, attachments)


def message_text(message: dict) -> dict[str, Any]:
    """One Gmail `format=full` message as the fields a reader needs."""
    payload = message.get("payload") or {}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict] = []
    _walk(payload, plain, html, attachments)
    body = "\n".join(p for p in plain if p.strip())
    if not body and html:
        body = _html_to_text("\n".join(html))
    headers = _headers(payload)
    return {
        "message_id": message.get("id"),
        "thread_id": message.get("threadId"),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "cc": headers.get("Cc", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "labels": message.get("labelIds") or [],
        "body": body.strip(),
        "attachments": attachments,
    }


def _clip(messages: list[dict], limit: int) -> tuple[list[dict], bool]:
    """Cap the total body text across messages, newest kept whole first.

    Newest first because in a thread the latest reply usually quotes what came
    before it, so it is the one message whose loss costs the most.
    """
    remaining = limit
    truncated = False
    for m in reversed(messages):
        body = m["body"]
        if len(body) > remaining:
            m["body"] = body[:max(remaining, 0)] + "\n[... trimmed]"
            truncated = True
        remaining -= len(body)
    return messages, truncated


@tool({
    "type": "function",
    "function": {
        "name": "gmail_search_threads",
        "description": (
            "[Gmail] Search the user's mailbox and list matching threads with "
            "sender, subject, date and a snippet. Accepts Gmail search syntax "
            "(e.g. 'from:alice is:unread newer_than:7d', 'subject:invoice'). "
            "An empty query lists the most recent threads."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query."},
                "max_results": {"type": "integer", "description": "How many threads (default from settings)."},
            },
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector=CONNECTOR)
@handles_google_errors
async def gmail_search_threads(args: Dict, context: Dict) -> str:
    cap = await alimit(context, "gmail_search_threads", "maxResults")
    requested = args.get("max_results")
    limit = min(int(requested), cap) if isinstance(requested, int) and requested > 0 else cap
    params: dict[str, Any] = {"maxResults": limit}
    if (args.get("query") or "").strip():
        params["q"] = args["query"].strip()
    listing = await get_json(context, f"{API}/threads", params=params)
    threads = listing.get("threads") or []

    gate = asyncio.Semaphore(_DETAIL_CONCURRENCY)

    async def detail(thread_id: str) -> dict:
        async with gate:
            data = await get_json(
                context, f"{API}/threads/{thread_id}",
                params=[("format", "metadata"),
                        *[("metadataHeaders", h) for h in _SUMMARY_HEADERS]],
            )
        messages = data.get("messages") or []
        first = _headers((messages[0] if messages else {}).get("payload") or {})
        last = messages[-1] if messages else {}
        labels = {label for m in messages for label in (m.get("labelIds") or [])}
        return {
            "thread_id": thread_id,
            "subject": first.get("Subject", ""),
            "from": first.get("From", ""),
            "date": _headers(last.get("payload") or {}).get("Date", ""),
            "snippet": last.get("snippet", ""),
            "message_count": len(messages),
            "unread": "UNREAD" in labels,
        }

    details = await asyncio.gather(*(detail(t["id"]) for t in threads))
    return json.dumps({
        "type": "gmail_threads",
        "query": params.get("q", ""),
        "count": len(details),
        "threads": details,
    })


@tool({
    "type": "function",
    "function": {
        "name": "gmail_get_thread",
        "description": (
            "[Gmail] Read a whole email thread: every message's sender, "
            "recipients, date, decoded body text and attachment names."
        ),
        "parameters": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}},
            "required": ["thread_id"],
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector=CONNECTOR)
@handles_google_errors
async def gmail_get_thread(args: Dict, context: Dict) -> str:
    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return "Error: 'thread_id' is required."
    data = await get_json(context, f"{API}/threads/{thread_id}", params={"format": "full"})
    messages = [message_text(m) for m in data.get("messages") or []]
    limit = await alimit(context, "gmail_get_thread", "charLimit")
    messages, truncated = _clip(messages, limit)
    return json.dumps({
        "type": "gmail_thread",
        "thread_id": thread_id,
        "messages": messages,
        "truncated": truncated,
    })


@tool({
    "type": "function",
    "function": {
        "name": "gmail_get_message",
        "description": "[Gmail] Read one email message by id, with its decoded body text.",
        "parameters": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector=CONNECTOR)
@handles_google_errors
async def gmail_get_message(args: Dict, context: Dict) -> str:
    message_id = (args.get("message_id") or "").strip()
    if not message_id:
        return "Error: 'message_id' is required."
    data = await get_json(context, f"{API}/messages/{message_id}", params={"format": "full"})
    message = message_text(data)
    limit = await alimit(context, "gmail_get_thread", "charLimit")
    [message], truncated = _clip([message], limit)
    return json.dumps({"type": "gmail_message", **message, "truncated": truncated})


@tool({
    "type": "function",
    "function": {
        "name": "gmail_list_labels",
        "description": "[Gmail] List the mailbox's labels (system and user-created) with their ids.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}, parallel=True, effect="read", connector=CONNECTOR)
@handles_google_errors
async def gmail_list_labels(args: Dict, context: Dict) -> str:
    data = await get_json(context, f"{API}/labels")
    labels = [
        {"id": label.get("id"), "name": label.get("name"), "type": label.get("type")}
        for label in data.get("labels") or []
    ]
    return json.dumps({"type": "gmail_labels", "labels": labels})


# -- writing ------------------------------------------------------------------

_COMPOSE_PROPERTIES = {
    "to": {"type": "string", "description": "Recipient address(es), comma-separated."},
    "subject": {"type": "string"},
    "body": {"type": "string", "description": "Plain-text body."},
    "cc": {"type": "string"},
    "bcc": {"type": "string"},
    "reply_to_message_id": {
        "type": "string",
        "description": "Gmail message id to reply to; keeps the reply in its thread.",
    },
}


def _addresses(value: str) -> str:
    """Normalise an address list, refusing anything that could inject headers."""
    if any(c in (value or "") for c in "\r\n"):
        raise GoogleAPIError("bad_request", "Addresses may not contain line breaks.")
    pairs = getaddresses([value or ""])
    return ", ".join(addr if not name else f"{name} <{addr}>" for name, addr in pairs if addr)


async def _compose(args: Dict, context: Dict) -> dict[str, Any]:
    """The `message` resource for a draft or a send."""
    to = _addresses(args.get("to") or "")
    body = args.get("body") or ""
    subject = args.get("subject") or ""
    if any(c in subject for c in "\r\n"):
        raise GoogleAPIError("bad_request", "The subject may not contain line breaks.")

    thread_id = None
    msg = EmailMessage()
    reply_to = (args.get("reply_to_message_id") or "").strip()
    if reply_to:
        original = await get_json(
            context, f"{API}/messages/{reply_to}",
            params=[("format", "metadata"), ("metadataHeaders", "Message-ID"),
                    ("metadataHeaders", "References"), ("metadataHeaders", "Subject"),
                    ("metadataHeaders", "From"), ("metadataHeaders", "Reply-To")],
        )
        headers = _headers(original.get("payload") or {})
        thread_id = original.get("threadId")
        message_id_header = headers.get("Message-ID") or headers.get("Message-Id")
        if message_id_header:
            msg["In-Reply-To"] = message_id_header
            msg["References"] = f"{headers.get('References', '')} {message_id_header}".strip()
        if not subject:
            original_subject = headers.get("Subject", "")
            subject = original_subject if original_subject.lower().startswith("re:") \
                else f"Re: {original_subject}"
        if not to:
            to = _addresses(headers.get("Reply-To") or headers.get("From") or "")

    if not to:
        raise GoogleAPIError("bad_request", "A recipient ('to') is required.")
    msg["To"] = to
    if args.get("cc"):
        msg["Cc"] = _addresses(args["cc"])
    if args.get("bcc"):
        msg["Bcc"] = _addresses(args["bcc"])
    msg["Subject"] = subject
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    resource: dict[str, Any] = {"raw": raw}
    if thread_id:
        resource["threadId"] = thread_id
    return resource


@tool({
    "type": "function",
    "function": {
        "name": "gmail_create_draft",
        "description": (
            "[Gmail] Save an email as a draft in the user's mailbox without "
            "sending it. Prefer this over sending unless the user clearly asked "
            "for the email to go out."
        ),
        "parameters": {
            "type": "object",
            "properties": _COMPOSE_PROPERTIES,
            "required": ["body"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible", connector=CONNECTOR)
@handles_google_errors
async def gmail_create_draft(args: Dict, context: Dict) -> str:
    message = await _compose(args, context)
    draft = await send_json(context, "POST", f"{API}/drafts", json={"message": message})
    return json.dumps({
        "status": "success",
        "draft_id": draft.get("id"),
        "message_id": (draft.get("message") or {}).get("id"),
        "text": "Draft saved. It has not been sent.",
    })


@tool({
    "type": "function",
    "function": {
        "name": "gmail_send_message",
        "description": (
            "[Gmail] Send an email from the user's account. This cannot be "
            "undone — only use when the user asked for the email to be sent."
        ),
        "parameters": {
            "type": "object",
            "properties": _COMPOSE_PROPERTIES,
            "required": ["body"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible", connector=CONNECTOR)
@handles_google_errors
async def gmail_send_message(args: Dict, context: Dict) -> str:
    message = await _compose(args, context)
    sent = await send_json(context, "POST", f"{API}/messages/send", json=message)
    return json.dumps({
        "status": "success",
        "message_id": sent.get("id"),
        "thread_id": sent.get("threadId"),
        "text": "Email sent.",
    })


async def _label_ids(context: Dict, names: list) -> list[str]:
    """Accept label ids or names; names resolve case-insensitively."""
    wanted = [str(n) for n in names or [] if str(n).strip()]
    if not wanted:
        return []
    data = await get_json(context, f"{API}/labels")
    by_name = {(label.get("name") or "").lower(): label.get("id") for label in data.get("labels") or []}
    ids = {label.get("id") for label in data.get("labels") or []}
    resolved = []
    for name in wanted:
        if name in ids:
            resolved.append(name)
        elif name.lower() in by_name:
            resolved.append(by_name[name.lower()])
        else:
            raise GoogleAPIError("not_found", f"No Gmail label named {name!r}.")
    return resolved


@tool({
    "type": "function",
    "function": {
        "name": "gmail_modify_labels",
        "description": (
            "[Gmail] Add or remove labels on a message or a whole thread — e.g. "
            "mark read (remove UNREAD), archive (remove INBOX), star (add STARRED). "
            "Labels may be given by name or id."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
                "thread_id": {"type": "string"},
                "add_labels": {"type": "array", "items": {"type": "string"}},
                "remove_labels": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible", connector=CONNECTOR)
@handles_google_errors
async def gmail_modify_labels(args: Dict, context: Dict) -> str:
    message_id = (args.get("message_id") or "").strip()
    thread_id = (args.get("thread_id") or "").strip()
    if bool(message_id) == bool(thread_id):
        return "Error: give exactly one of 'message_id' or 'thread_id'."
    add = await _label_ids(context, args.get("add_labels"))
    remove = await _label_ids(context, args.get("remove_labels"))
    if not add and not remove:
        return "Error: give at least one label to add or remove."
    target = f"{API}/messages/{message_id}" if message_id else f"{API}/threads/{thread_id}"
    await send_json(
        context, "POST", f"{target}/modify",
        json={"addLabelIds": add, "removeLabelIds": remove},
    )
    return json.dumps({"status": "success", "added": add, "removed": remove})


@tool({
    "type": "function",
    "function": {
        "name": "gmail_trash_message",
        "description": "[Gmail] Move a message to the Trash. It can be restored from Trash for 30 days.",
        "parameters": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible", connector=CONNECTOR)
@handles_google_errors
async def gmail_trash_message(args: Dict, context: Dict) -> str:
    message_id = (args.get("message_id") or "").strip()
    if not message_id:
        return "Error: 'message_id' is required."
    await send_json(context, "POST", f"{API}/messages/{message_id}/trash")
    return json.dumps({"status": "success", "text": "Moved to Trash."})
