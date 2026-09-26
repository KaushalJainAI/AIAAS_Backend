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

from tools_config.overlay import alimit

logger = logging.getLogger(__name__)


def _scope(context: Dict) -> Any:
    """The caller's `FileScope`, or None when file access was not granted."""
    return context.get("file_scope")


def _no_scope() -> str:
    return json.dumps({
        "error": "This agent has no file access. Turn on 'Read and write files' "
                 "and set a file access level in its settings."
    })


#: Shared by write_file and edit_file: the optimistic-concurrency check.
_EXPECTED_VERSION = {
    "type": "string",
    "description": (
        "The `version` from your last read of this file. If given and the file "
        "changed since, the write is refused instead of overwriting someone "
        "else's change. Recommended when other agents share the folder."
    ),
}


def _int(value: Any) -> int:
    """A model-supplied integer, or 0. "20 lines" gets the default, not a crash."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
            "Returns entries directly inside the path; pass depth (up to 3) to "
            "also get a `tree` of everything that many levels down."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to list, relative to your workspace root. Defaults to the root.",
                },
                "depth": {
                    "type": "integer",
                    "description": "How many levels to include in `tree` (1-3). Defaults to 1: this directory only.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}, requires="files", parallel=True, effect="read")
async def list_files(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(
        context, vfs.list_dir, args.get("path") or "/",
        limit=await alimit(context, "list_files", "maxEntries"),
        depth=_int(args.get("depth")) or 1)


@tool({
    "type": "function",
    "function": {
        "name": "read_file",
        "description": (
            "Read the text of one file in your workspace. Long files come back "
            "in windows: if the result says it was truncated, call again with "
            "the offset it names to continue. Pass start_line/end_line to read "
            "numbered lines instead — the best way to look at the lines "
            "find_files pointed to. Returns the file's text, not a summary of "
            "it, and a `version` you can pass to write_file/edit_file as "
            "expected_version so your write is refused if someone else changed "
            "the file after you read it."
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
                "start_line": {
                    "type": "integer",
                    "description": "First line to read (1-based). Switches to numbered-line output.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line to read, inclusive. Defaults to the end of the file.",
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
        offset=_int(args.get("offset")),
        window=await alimit(context, "read_file", "windowChars"),
        start_line=_int(args.get("start_line")) or None,
        end_line=_int(args.get("end_line")) or None,
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
                "expected_version": _EXPECTED_VERSION,
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
        max_chars=await alimit(context, "write_file", "maxChars"),
        expected_version=args.get("expected_version") or None,
    )


@tool({
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Change part of an existing file, leaving the rest exactly as it "
            "is. Prefer this over write_file for any change to a file that "
            "already has content — write_file replaces the whole document, so "
            "using it to change one line means re-emitting every other line "
            "and risks losing them. old_text must match the file character for "
            "character, including indentation and line breaks, and must appear "
            "exactly once unless you pass replace_all."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File to edit, relative to your workspace root. It must already exist.",
                },
                "old_text": {
                    "type": "string",
                    "description": (
                        "The exact text to replace, copied verbatim from the file. "
                        "Include enough surrounding text to make it unique."
                    ),
                },
                "new_text": {
                    "type": "string",
                    "description": "What to put in its place. Empty string deletes the matched text.",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": (
                        "Replace every occurrence instead of requiring exactly one. "
                        "Use for a rename that runs through the file."
                    ),
                },
                "expected_version": _EXPECTED_VERSION,
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
}, requires="files", sensitive=True, effect="reversible")
async def edit_file(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(
        context, vfs.edit_file, args.get("path") or "",
        args.get("old_text") or "", args.get("new_text") or "",
        replace_all=bool(args.get("replace_all")),
        max_chars=await alimit(context, "edit_file", "maxChars"),
        expected_version=args.get("expected_version") or None,
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


_MOVE_COPY_PARAMS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "What to move or copy, relative to your workspace root.",
        },
        "to": {
            "type": "string",
            "description": "Destination directory, or the new full path including the name.",
        },
    },
    "required": ["path", "to"],
    "additionalProperties": False,
}


@tool({
    "type": "function",
    "function": {
        "name": "move_file",
        "description": (
            "Move or rename a file or directory in your workspace. It keeps its "
            "identity and version history — use this instead of reading, "
            "rewriting and deleting. If `to` is an existing directory the item "
            "goes inside it; otherwise `to` is the new full path (missing "
            "parent directories are created). Refuses if the destination name "
            "is already taken."
        ),
        "parameters": _MOVE_COPY_PARAMS,
    },
}, requires="files", sensitive=True, effect="reversible")
async def move_file(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(context, vfs.move, args.get("path") or "", args.get("to") or "")


@tool({
    "type": "function",
    "function": {
        "name": "copy_file",
        "description": (
            "Copy one file (including .docx, .xlsx and .pptx) to a new path in "
            "your workspace. If `to` is an existing directory the copy keeps "
            "its name; otherwise `to` is the new full path. Refuses if the "
            "destination name is already taken. Files only, not directories."
        ),
        "parameters": _MOVE_COPY_PARAMS,
    },
}, requires="files", sensitive=True, effect="reversible")
async def copy_file(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(context, vfs.copy, args.get("path") or "", args.get("to") or "")


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
            "means the text is literally there. Each match lists up to three "
            "`snippets` with line numbers; read around them with "
            "read_file(start_line=...)."
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
    if not limit:
        limit = await alimit(context, "find_files", "maxEntries")
    return await _run(context, vfs.find, args.get("query") or "", limit=limit)


@tool({
    "type": "function",
    "function": {
        "name": "file_versions",
        "description": (
            "List the earlier versions of a file: every overwrite — by the user "
            "in an app, by an agent, or by a restore — keeps what the file held "
            "before it. Use this before restore_file_version, or to answer "
            "'what did this say yesterday'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File whose history to list, relative to your workspace root.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}, requires="files", parallel=True, effect="read")
async def file_versions(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(context, vfs.file_versions, args.get("path") or "")


@tool({
    "type": "function",
    "function": {
        "name": "restore_file_version",
        "description": (
            "Put an earlier version of a file back, from file_versions. The "
            "current contents are kept as a version first, so a restore can "
            "itself be undone. Works for every file type, including decks, "
            "workbooks and Word files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File to restore, relative to your workspace root.",
                },
                "version_id": {
                    "type": "integer",
                    "description": "The version_id file_versions listed.",
                },
            },
            "required": ["path", "version_id"],
            "additionalProperties": False,
        },
    },
}, requires="files", sensitive=True, effect="reversible")
async def restore_file_version(args: Dict, context: Dict) -> str:
    from inference import vfs

    if _scope(context) is None:
        return _no_scope()
    try:
        version_id = int(args.get("version_id"))
    except (TypeError, ValueError):
        return json.dumps({"error": "version_id must be the number file_versions listed."})
    return await _run(context, vfs.restore_file_version, args.get("path") or "", version_id)


@tool({
    "type": "function",
    "function": {
        "name": "export_file",
        "description": (
            "Save a file in another format beside it, without changing the "
            "original: a Word file, Markdown or text as PDF; Markdown or text "
            "as Word; a Word file as Markdown or text; a deck as PDF; a "
            "workbook as CSV; a CSV as a workbook. Never overwrites — a taken "
            "name becomes 'name (2).ext' and the result names the real path. "
            "Use this rather than re-rendering a file to change its format."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File to export, relative to your workspace root.",
                },
                "format": {
                    "type": "string",
                    "enum": ["pdf", "docx", "md", "txt", "csv", "xlsx"],
                },
                "target": {
                    "type": "string",
                    "description": "Where to save it. Defaults to the same folder and name with the new extension.",
                },
            },
            "required": ["path", "format"],
            "additionalProperties": False,
        },
    },
}, requires="files", effect="reversible")
async def export_file(args: Dict, context: Dict) -> str:
    from inference import vfs

    return await _run(
        context, vfs.export_file, args.get("path") or "", args.get("format") or "",
        target=args.get("target") or "",
    )


@tool({
    "type": "function",
    "function": {
        "name": "edit_document",
        "description": (
            "Change a Word file block by block: insert, replace or delete "
            "blocks by 0-based index, or find and replace text. Works on "
            "uploads too — an uploaded file is converted to an editable "
            "document first (its original stays in version history). The "
            "current contents are kept as a version, so this can be undone. "
            "Blocks are objects like {type: paragraph, text} — read the file "
            "first to see their indices."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Word file to edit, relative to your workspace root.",
                },
                "ops": {
                    "type": "array",
                    "description": "Edits: {op: insert|replace|delete|find_replace, "
                                   "index, blocks, count, find, replace, replace_all}.",
                    "items": {"type": "object"},
                },
            },
            "required": ["path", "ops"],
            "additionalProperties": False,
        },
    },
}, requires="files", sensitive=True, effect="reversible")
async def edit_document(args: Dict, context: Dict) -> str:
    from inference import vfs

    if _scope(context) is None:
        return _no_scope()
    ops = args.get("ops")
    if not isinstance(ops, list):
        return json.dumps({"error": "ops must be a list of edits."})
    return await _run(context, vfs.edit_document, args.get("path") or "", ops)


@tool({
    "type": "function",
    "function": {
        "name": "edit_deck",
        "description": (
            "Change a deck slide by slide: add, remove, move or duplicate "
            "slides, or set a slide's fields. Works on uploads too — an "
            "uploaded deck is converted to an editable one first (its "
            "original stays in version history). The current contents are "
            "kept as a version, so this can be undone. Slides are 0-based; "
            "read the file first to see them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Deck to edit, relative to your workspace root.",
                },
                "ops": {
                    "type": "array",
                    "description": "Edits: {op: add|remove|move|duplicate|set, "
                                   "index, to, slide, fields}.",
                    "items": {"type": "object"},
                },
            },
            "required": ["path", "ops"],
            "additionalProperties": False,
        },
    },
}, requires="files", sensitive=True, effect="reversible")
async def edit_deck(args: Dict, context: Dict) -> str:
    from inference import vfs

    if _scope(context) is None:
        return _no_scope()
    ops = args.get("ops")
    if not isinstance(ops, list):
        return json.dumps({"error": "ops must be a list of edits."})
    return await _run(context, vfs.edit_deck, args.get("path") or "", ops)
