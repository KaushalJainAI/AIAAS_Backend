"""
Google Drive and Google Sheets, over `drive/v3` and `sheets/v4`.

Reading a Drive file is three different operations wearing one name, and the
tool hides that because a model cannot be expected to know it: a Google Doc
has no bytes and must be *exported*, a Sheet exports as CSV of its first tab
only (so the tool says so and points at `sheets_get_values`), and an uploaded
PDF or .docx is downloaded and run through the same extractor the knowledge
base uses — so a file reads the same whether the user uploaded it here or
keeps it in Drive.

Sheets lives here rather than in its own module because it is two tools that
share Drive's file ids and nothing else; a module each would be one file per
tool.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from typing import Any, Dict
from urllib.parse import quote

from asgiref.sync import sync_to_async

from tools_config.overlay import alimit

from ..registry import tool
from .client import GoogleAPIError, download, get_json, handles_google_errors, send_json

DRIVE = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"

_FILE_FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,owners(displayName,emailAddress)"

#: Google-native types and what they export as. Anything else under
#: `application/vnd.google-apps.` (forms, sites, shortcuts) has no text export.
_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}

_TEXT_MIME_PREFIXES = ("text/", "application/json", "application/xml", "application/csv")


def _file_summary(f: dict) -> dict[str, Any]:
    return {
        "file_id": f.get("id"),
        "name": f.get("name"),
        "mime_type": f.get("mimeType"),
        "modified": f.get("modifiedTime"),
        "size": int(f["size"]) if str(f.get("size") or "").isdigit() else None,
        "link": f.get("webViewLink"),
        "owners": [o.get("displayName") or o.get("emailAddress") for o in f.get("owners") or []],
    }


def _escape(value: str) -> str:
    """A string literal inside a Drive `q` expression."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _extract(body: bytes, name: str, mime: str) -> str:
    """Bytes of an uploaded file -> text, through the knowledge-base extractor."""
    from inference.utils import DocumentProcessor, normalize_file_type

    file_type = normalize_file_type(name, mime)
    suffix = os.path.splitext(name or "")[1] or ""
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(body)
        return DocumentProcessor.extract_text_from_file(path, file_type) or ""
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@tool({
    "type": "function",
    "function": {
        "name": "drive_search_files",
        "description": (
            "[Google Drive] Find files in the user's Drive whose name or contents "
            "contain the given text. Returns ids to pass to drive_read_file_content."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to look for."},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector="google-drive")
@handles_google_errors
async def drive_search_files(args: Dict, context: Dict) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: 'query' is required."
    cap = await alimit(context, "drive_search_files", "maxResults")
    requested = args.get("max_results")
    limit = min(int(requested), cap) if isinstance(requested, int) and requested > 0 else cap
    q = f"(name contains '{_escape(query)}' or fullText contains '{_escape(query)}') and trashed = false"
    data = await get_json(context, f"{DRIVE}/files", params={
        "q": q, "pageSize": limit, "fields": f"files({_FILE_FIELDS})",
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
    })
    files = [_file_summary(f) for f in data.get("files") or []]
    return json.dumps({"type": "drive_files", "query": query, "count": len(files), "files": files})


@tool({
    "type": "function",
    "function": {
        "name": "drive_list_recent_files",
        "description": "[Google Drive] List the files in the user's Drive modified most recently.",
        "parameters": {
            "type": "object",
            "properties": {"max_results": {"type": "integer"}},
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector="google-drive")
@handles_google_errors
async def drive_list_recent_files(args: Dict, context: Dict) -> str:
    cap = await alimit(context, "drive_search_files", "maxResults")
    requested = args.get("max_results")
    limit = min(int(requested), cap) if isinstance(requested, int) and requested > 0 else cap
    data = await get_json(context, f"{DRIVE}/files", params={
        "q": "trashed = false", "orderBy": "modifiedTime desc", "pageSize": limit,
        "fields": f"files({_FILE_FIELDS})",
    })
    files = [_file_summary(f) for f in data.get("files") or []]
    return json.dumps({"type": "drive_files", "count": len(files), "files": files})


@tool({
    "type": "function",
    "function": {
        "name": "drive_get_file_metadata",
        "description": "[Google Drive] Get one file's name, type, size, owners, modified time and link.",
        "parameters": {
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector="google-drive")
@handles_google_errors
async def drive_get_file_metadata(args: Dict, context: Dict) -> str:
    file_id = (args.get("file_id") or "").strip()
    if not file_id:
        return "Error: 'file_id' is required."
    f = await get_json(context, f"{DRIVE}/files/{quote(file_id)}", params={
        "fields": _FILE_FIELDS, "supportsAllDrives": "true",
    })
    return json.dumps({"type": "drive_file", **_file_summary(f)})


@tool({
    "type": "function",
    "function": {
        "name": "drive_read_file_content",
        "description": (
            "[Google Drive] Read a Drive file's text: Google Docs and Slides as "
            "text, Google Sheets as CSV of the first tab, and uploaded PDF, Word, "
            "text or CSV files. Use 'offset' to read further into a long file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "offset": {"type": "integer", "description": "Character offset to start from."},
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector="google-drive")
@handles_google_errors
async def drive_read_file_content(args: Dict, context: Dict) -> str:
    file_id = (args.get("file_id") or "").strip()
    if not file_id:
        return "Error: 'file_id' is required."
    offset = args.get("offset") if isinstance(args.get("offset"), int) else 0
    offset = max(offset, 0)

    meta = await get_json(context, f"{DRIVE}/files/{quote(file_id)}", params={
        "fields": "id,name,mimeType,size", "supportsAllDrives": "true",
    })
    mime = meta.get("mimeType") or ""
    name = meta.get("name") or ""
    note = ""

    if mime in _EXPORTS:
        body = await download(
            context, f"{DRIVE}/files/{quote(file_id)}/export",
            params={"mimeType": _EXPORTS[mime]},
        )
        text = body.decode("utf-8", errors="replace")
        if mime.endswith("spreadsheet"):
            note = "Only the first sheet is exported; use sheets_get_values for other tabs."
    elif mime.startswith("application/vnd.google-apps."):
        raise GoogleAPIError(
            "unsupported", f"'{name}' is a {mime.rsplit('.', 1)[-1]}, which has no text to read.",
        )
    else:
        body = await download(
            context, f"{DRIVE}/files/{quote(file_id)}",
            params={"alt": "media", "supportsAllDrives": "true"},
        )
        if mime.startswith(_TEXT_MIME_PREFIXES):
            text = body.decode("utf-8", errors="replace")
        else:
            text = await sync_to_async(_extract)(body, name, mime)
            if not text.strip():
                raise GoogleAPIError(
                    "unsupported", f"No readable text could be extracted from '{name}' ({mime}).",
                )

    limit = await alimit(context, "drive_read_file_content", "charLimit")
    window = text[offset:offset + limit]
    end = offset + len(window)
    return json.dumps({
        "type": "drive_file_content",
        "file_id": file_id,
        "name": name,
        "mime_type": mime,
        "offset": offset,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
        "note": note,
        "content": window,
    })


@tool({
    "type": "function",
    "function": {
        "name": "drive_create_file",
        "description": (
            "[Google Drive] Create a new file in the user's Drive from text. Set "
            "as_google_doc to create an editable Google Doc instead of a plain "
            "text file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "content": {"type": "string"},
                "as_google_doc": {"type": "boolean"},
                "folder_id": {"type": "string", "description": "Parent folder id; defaults to My Drive."},
            },
            "required": ["name", "content"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible", connector="google-drive")
@handles_google_errors
async def drive_create_file(args: Dict, context: Dict) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return "Error: 'name' is required."
    metadata: dict[str, Any] = {"name": name}
    if args.get("as_google_doc"):
        metadata["mimeType"] = "application/vnd.google-apps.document"
    if (args.get("folder_id") or "").strip():
        metadata["parents"] = [args["folder_id"].strip()]

    boundary = f"aiaas-{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n"
        f"{args.get('content') or ''}\r\n--{boundary}--"
    ).encode("utf-8")
    created = await send_json(
        context, "POST", UPLOAD,
        params={"uploadType": "multipart", "fields": _FILE_FIELDS, "supportsAllDrives": "true"},
        content=body,
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    return json.dumps({"status": "success", **_file_summary(created)})


# -- Sheets ---------------------------------------------------------------------

@tool({
    "type": "function",
    "function": {
        "name": "sheets_get_values",
        "description": (
            "[Google Sheets] Read cell values from a spreadsheet. 'range' is A1 "
            "notation such as 'Sheet1!A1:D50'; omit it to get the tab names and "
            "the first tab's values."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string"},
            },
            "required": ["spreadsheet_id"],
            "additionalProperties": False,
        },
    },
}, parallel=True, effect="read", connector="google-sheets")
@handles_google_errors
async def sheets_get_values(args: Dict, context: Dict) -> str:
    spreadsheet_id = (args.get("spreadsheet_id") or "").strip()
    if not spreadsheet_id:
        return "Error: 'spreadsheet_id' is required."
    range_ = (args.get("range") or "").strip()
    tabs: list[str] = []
    if not range_:
        meta = await get_json(context, f"{SHEETS}/{quote(spreadsheet_id)}", params={
            "fields": "properties.title,sheets.properties.title",
        })
        tabs = [s["properties"]["title"] for s in meta.get("sheets") or []]
        if not tabs:
            return json.dumps({"type": "sheet_values", "tabs": [], "values": []})
        range_ = f"'{tabs[0]}'"
    data = await get_json(context, f"{SHEETS}/{quote(spreadsheet_id)}/values/{quote(range_)}")
    values = data.get("values") or []
    limit = await alimit(context, "sheets_get_values", "maxRows")
    return json.dumps({
        "type": "sheet_values",
        "range": data.get("range", range_),
        "tabs": tabs,
        "row_count": len(values),
        "truncated": len(values) > limit,
        "values": values[:limit],
    })


@tool({
    "type": "function",
    "function": {
        "name": "sheets_update_values",
        "description": (
            "[Google Sheets] Write values into a range of a spreadsheet, "
            "overwriting what is there. 'values' is a list of rows. Values are "
            "interpreted as if typed by a user, so formulas like '=SUM(A1:A3)' work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string", "description": "A1 notation, e.g. 'Sheet1!A1'."},
                "values": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}},
                },
            },
            "required": ["spreadsheet_id", "range", "values"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="reversible", connector="google-sheets")
@handles_google_errors
async def sheets_update_values(args: Dict, context: Dict) -> str:
    spreadsheet_id = (args.get("spreadsheet_id") or "").strip()
    range_ = (args.get("range") or "").strip()
    values = args.get("values")
    if not spreadsheet_id or not range_ or not isinstance(values, list):
        return "Error: 'spreadsheet_id', 'range' and 'values' are required."
    result = await send_json(
        context, "PUT", f"{SHEETS}/{quote(spreadsheet_id)}/values/{quote(range_)}",
        params={"valueInputOption": "USER_ENTERED"},
        json={"range": range_, "majorDimension": "ROWS", "values": values},
    )
    return json.dumps({
        "status": "success",
        "updated_range": result.get("updatedRange"),
        "updated_cells": result.get("updatedCells"),
    })
