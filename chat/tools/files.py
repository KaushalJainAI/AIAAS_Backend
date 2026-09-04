"""
File tools over the agent's virtual filesystem (`inference/vfs.py`).

These are **not** the `list_files` / `read_file` / `write_file` / `delete_file`
that were removed from chat. Those reached the host filesystem from a chat turn.
These reach rows in the caller's own `Folder`/`Document` tree, through a scope
built from the agent's `fileAccess` setting, and cannot name a path on any disk.
`chat/tests/test_rework.py::RemovedCapabilityTests` records that distinction and
pins the half that is still true — the host-filesystem capability stays gone.

They are offered to **agent runs only**. `requires="files"` is unmet in chat by
construction (`_requirement_met` answers False for it), because a chat turn has
no `fileAccess` setting to build a scope from; the agent toolbox filters by
grant name instead of by requirement, so the `fileOps` grant is what turns them
on. That is the whole reason the requirement exists rather than a name check in
a distant filter.

Every tool takes and returns paths relative to the agent's own root, so a model
never sees an id it has to carry, and never sees a folder it cannot reach.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from asgiref.sync import sync_to_async

from .registry import tool

logger = logging.getLogger(__name__)


def _scope(context: Dict) -> Any:
    """The caller's `FileScope`, or None when file access was not granted."""
    return context.get("file_scope")


def _no_scope() -> str:
    return json.dumps({
        "error": "This agent has no file access. Turn on 'Read and write files' "
                 "and set a file access level in its settings."
    })


async def _run(context: Dict, fn, *args, **kwargs) -> str:
    """Call one `vfs` function off the event loop and render it for the model.

    `VfsError` is an answer, not a crash: its message is written to be read by a
    model and says what to do next, so it comes back as `error` rather than as a
    traceback the model has to interpret.
    """
    from inference.vfs import VfsError

    scope = _scope(context)
    if scope is None:
        return _no_scope()

    try:
        result = await sync_to_async(fn)(scope, *args, **kwargs)
    except VfsError as e:
        return json.dumps({"error": str(e)})
    except Exception:
        logger.exception("[FileTools] %s failed", getattr(fn, "__name__", fn))
        return json.dumps({"error": "That file operation failed unexpectedly."})

    return json.dumps(result, default=str)


@tool({
    "type": "function",
    "function": {
        "name": "list_files",
        "description": (
            "List the directories and files at a path in your workspace. Call "
            "this before reading or writing when you are not certain what is "
            "there — paths are case-sensitive and guessing wastes a turn. "
            "Returns entries directly inside the path only, not recursively."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list, relative to your workspace root. Defaults to the root.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}, requires="files", parallel=True, effect="read")
async def list_files(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(context, vfs.list_dir, args.get("path") or "/")


@tool({
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read the text of one file in your workspace. Long files come back "
            "in windows: if the result says it was truncated, call again with "
            "the offset it names to continue. Returns the file's text, not a "
            "summary of it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File to read, relative to your workspace root.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Character offset to start from. Use the offset a truncated read names.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}, requires="files", parallel=True, effect="read")
async def read_file(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(
        context, vfs.read_file, args.get("path") or "",
        offset=args.get("offset") or 0,
    )


@tool({
    "type": "function",
    "function": {
        "name": "write_file",
        "description": (
            "Write text to a file in your workspace, creating it and any "
            "missing parent directories. Overwrites by default — pass "
            "append=true to add to the end instead. The file becomes visible to "
            "the user in their own file browser, so name it something they "
            "would recognise."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File to write, relative to your workspace root. Include an extension (.md, .txt, .json, .csv).",
                },
                "content": {
                    "type": "string",
                    "description": "The full text to write.",
                },
                "append": {
                    "type": "boolean",
                    "description": "Append to the file instead of replacing its contents.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
}, requires="files", sensitive=True, effect="reversible")
async def write_file(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(
        context, vfs.write_file, args.get("path") or "",
        args.get("content") or "", append=bool(args.get("append")),
    )


@tool({
    "type": "function",
    "function": {
        "name": "make_directory",
        "description": (
            "Create a directory in your workspace, including any missing "
            "parents. Rarely needed on its own — write_file already creates the "
            "directories its path names."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to create, relative to your workspace root.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}, requires="files", sensitive=True, effect="reversible")
async def make_directory(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(context, vfs.make_dir, args.get("path") or "")


@tool({
    "type": "function",
    "function": {
        "name": "delete_file",
        "description": (
            "Move a file or directory in your workspace to the user's recycle "
            "bin, where they can restore it. Deleting a directory takes "
            "everything inside it. Prefer overwriting a file with write_file "
            "over deleting and recreating it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File or directory to delete, relative to your workspace root.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}, requires="files", sensitive=True, effect="reversible")
async def delete_file(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(context, vfs.delete, args.get("path") or "")


@tool({
    "type": "function",
    "function": {
        "name": "find_files",
        "description": (
            "Find files anywhere in your workspace by name or by text inside "
            "them. Use this instead of listing directories one at a time when "
            "you know roughly what a file is called or what it says. This is "
            "plain substring matching over your own files — it is not a "
            "knowledge base search and does not rank by relevance, so a match "
            "means the text is literally there."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to look for in file names and file contents. At least two characters.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum matches to return. Defaults to the workspace listing limit.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}, requires="files", parallel=True, effect="read")
async def find_files(args: Dict, context: Dict) -> str:
    from inference import vfs

    try:
        limit = int(args.get("limit") or 0)
    except (TypeError, ValueError):
        # A model that sends "20 files" gets the default, not a crash: the
        # cap is a bound we own, not something the caller has to get right.
        limit = 0
    return await _run(context, vfs.find, args.get("query") or "", limit=limit)
