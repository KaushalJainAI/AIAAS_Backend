"""
The Python sandbox tools. In production the code runs in a hardened sidecar
container (`sandbox_service/`) — real kernel-level confinement, no network
egress, no secrets, and C extensions (numpy/pandas) available. Local dev with
no sidecar falls back to an in-process AST-guarded engine, which is weaker and
for development only. Engine selection lives in `sandbox/engine.py`.

Two tools share that one engine, split by effect rather than by implementation:

- `execute_python` (read-only) runs code with no filesystem. It stays in `plan`
  mode and in every autonomy level, which is why it must never write a file.
- `run_python_on_files` (reversible) hands input files to the same sandbox and
  collects declared outputs back into the caller's file scope. It is withheld
  where there is no scope and in `plan` mode, exactly like the other file
  tools, because a file it writes is a side effect.
"""
from __future__ import annotations

import json
import logging
import re

from typing import Any, Dict

from .registry import tool

logger = logging.getLogger(__name__)

#: How much sandbox stdout to hand back when the user has not said otherwise.
#: A runaway loop printing megabytes would blow the context window in a single
#: tool call. The user-facing knob is `tools_config.settings_schema`, whose
#: default is this number; this stays as the floor under a failed read.
MAX_CODE_OUTPUT_CHARS = 20_000

#: A bare sandbox file name: no separators, no NUL, not `.`/`..`, no leading
#: dot. Mirrors `sandbox_service/executor.py::_NAME` so the tool refuses before
#: the engine does — with a message naming the VFS path to fix, not a bare
#: sidecar 400.
_NAME = re.compile(r'^(?!\.)[^/\\\x00]{1,128}$')


def _jsonable(value: Any) -> Any:
    """Sandbox code returns anything; the transcript only carries JSON."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return repr(value)


@tool({
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": "Run Python in a sandbox and get its output back. No network access. The standard library is available, plus numpy and pandas for numeric and tabular work. Use it for arithmetic, data manipulation, parsing, analysis and simulation instead of computing in your head — you are unreliable at arithmetic and this is not. Assign the answer to a variable named `result`, or print it. There is no plotting library: to chart what you computed, print the numbers and pass them to `render_chart`.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python source to run."
                    }
                },
                "required": [
                    "code"
                ],
                "additionalProperties": False
            }
        }
    }, effect="read")
async def execute_python(args: Dict, context: Dict) -> str:
    """Run code through the same sandbox the Code node and agents use.

    Reusing the `sandbox` package rather than adding a second sandbox is
    deliberate: a second one is a second thing to get wrong, and this one is
    already the audited path (docs/SANDBOX_EXECUTION.md). It is also why
    this is not the `execute_python_code` tool that was removed — that one
    ran `exec` against the process, and `chat/tests/test_rework.py` asserts it
    stays unreachable.

    Failures come back as a plain `Error: ...` string rather than JSON so a
    model reading the transcript cannot mistake a traceback for a result.
    """
    from sandbox.engine import arun_code

    code = (args.get("code") or "").strip()
    if not code:
        return "Error: 'code' is required."

    from tools_config.overlay import alimit

    cap = await alimit(context, "execute_python", "outputLimit")

    # `sandbox.engine` picks the configured engine (hardened sidecar in prod,
    # in-process fallback in dev) and returns the same envelope either way.
    outcome = await arun_code(code)

    if not outcome.get("success"):
        detail = outcome.get("error") or "Execution failed."
        if outcome.get("stderr"):
            detail = f"{detail}\n{outcome['stderr']}"
        return f"Error: {detail}"[:cap]

    return json.dumps({
        "type": "code_execution",
        "result": _jsonable(outcome.get("result")),
        "stdout": (outcome.get("output") or "")[:cap],
    })[:cap]


def _bare_name(value: Any) -> str:
    return str(value or '').strip()


@tool({
    "type": "function",
    "function": {
        "name": "run_python_on_files",
        "description": (
            "Run Python in the sandbox with files: read input files from your "
            "workspace, compute with the full standard library plus numpy and "
            "pandas, and collect output files back into your workspace. "
            "Inputs are paths in your workspace (e.g. \"/Chat/raw.csv\"); they "
            "appear in the sandbox under their file names, so open(\"raw.csv\") "
            "reads \"/Chat/raw.csv\". Outputs are bare file names your code "
            "writes (e.g. \"clean.xlsx\"); each is saved into your own folder "
            "and reported with its workspace path. For more than a few thousand "
            "rows, prefer this over pasting file contents into execute_python. "
            "No network access. There is no plotting library: to chart what you "
            "computed, build the file and pass its numbers to render_chart, or "
            "use render_workbook for a spreadsheet with a native Excel chart."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python source to run. Use relative bare file names.",
                },
                "inputs": {
                    "type": "array",
                    "description": "Workspace paths to hand to the code (at most 5).",
                    "items": {"type": "string"},
                },
                "outputs": {
                    "type": "array",
                    "description": "Bare file names the code writes and should be kept (at most 5).",
                    "items": {"type": "string"},
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}, requires="files", effect="reversible")
async def run_python_on_files(args: Dict, context: Dict) -> str:
    """The file bridge over the same sandbox `execute_python` uses.

    Split into its own tool rather than as params on `execute_python` because
    the two have different effects: `execute_python` is declared `read` so it
    survives `plan` mode (the plan critic computes there), while anything that
    writes files must be `reversible` so `plan` withholds it. One tool cannot
    be both. The grant is the same (`codeExecution`); the file scope comes
    from `fileAccess`, exactly as for `fileOps`.
    """
    from asgiref.sync import sync_to_async

    from sandbox.engine import arun_code
    from workflow_backend.thresholds import (
        SANDBOX_FILE_MAX_BYTES,
        SANDBOX_FILE_MAX_FILES,
    )

    scope = context.get("file_scope")
    if scope is None:
        return json.dumps({
            "error": "There is no file workspace here, so files cannot be handed "
                     "to the sandbox. An agent needs file access turned on in its "
                     "settings."
        })

    code = (args.get("code") or "").strip()
    if not code:
        return "Error: 'code' is required."

    raw_inputs = args.get("inputs") or []
    raw_outputs = args.get("outputs") or []
    if not isinstance(raw_inputs, list) or not isinstance(raw_outputs, list):
        return json.dumps({"error": "'inputs' and 'outputs' must be lists."})
    from tools_config.overlay import alimit as _sandbox_alimit
    from tools_config.settings_schema import _SANDBOX_MAX_FILES as _sandbox_max

    try:
        file_cap = await _sandbox_alimit(context, "run_python_on_files", "maxFiles")
    except KeyError:
        file_cap = _sandbox_max
    if len(raw_inputs) > file_cap or len(raw_outputs) > file_cap:
        return json.dumps({
            "error": f"At most {file_cap} input and "
                     f"{file_cap} output files per run."
        })

    inputs = [_bare_name(v) for v in raw_inputs if _bare_name(v)]
    outputs = [_bare_name(v) for v in raw_outputs if _bare_name(v)]
    for name in outputs:
        if not _NAME.match(name) or name in ('.', '..'):
            return json.dumps({
                "error": f"{name!r} is not a plain file name. Outputs must be "
                         f"bare file names your code writes, e.g. \"clean.xlsx\"."
            })

    if not scope.writable and outputs:
        return json.dumps({
            "error": "This agent has read-only file access, so it cannot collect "
                     "output files. Report what you would have written instead."
        })

    from tools_config.overlay import alimit

    cap = await alimit(context, "execute_python", "outputLimit")

    try:
        files = await sync_to_async(_load_inputs)(scope, inputs)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    outcome = await arun_code(code, files=files, collect=outputs)

    if not outcome.get("success"):
        detail = outcome.get("error") or "Execution failed."
        if outcome.get("stderr"):
            detail = f"{detail}\n{outcome['stderr']}"
        return f"Error: {detail}"[:cap]

    saved: list[dict] = []
    try:
        saved = await sync_to_async(_save_outputs)(scope, outcome.get("files_out") or {})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})

    return json.dumps({
        "type": "code_execution",
        "result": _jsonable(outcome.get("result")),
        "stdout": (outcome.get("output") or "")[:cap],
        "files": saved,
        **({"missing": outcome.get("missing")}
           if outcome.get("missing") else {}),
        **({"unsaved": outcome.get("unsaved"),
            "note": "These files were written but not declared in 'outputs', "
                    "so they were not kept. Declare them to keep them."}
           if outcome.get("unsaved") else {}),
    }, default=str)[:cap]


def _load_inputs(scope, paths: list[str]) -> dict[str, bytes]:
    """Workspace paths -> sandbox inputs (`{bare name: bytes}`). Sync (DB)."""
    from workflow_backend.thresholds import SANDBOX_FILE_MAX_BYTES

    from inference import vfs

    out: dict[str, bytes] = {}
    for vfs_path in paths:
        parent_parts, leaf = vfs._split_leaf(scope, vfs_path)
        folder = vfs._folder_at(scope, parent_parts)
        doc = vfs._document_in(scope, folder, leaf)
        if doc is None:
            raise ValueError(
                f"No such file: {vfs.render(scope, parent_parts + [leaf])}. "
                f"List the directory to see what is there."
            )
        # Any stored bytes go in as bytes — including a format we have no
        # reader for, which is the case `execute_python` is the answer to.
        if doc.file:
            with doc.file.open('rb') as handle:
                data = handle.read(SANDBOX_FILE_MAX_BYTES + 1)
        else:
            data = (doc.content_text or '').encode('utf-8')
        if len(data) > SANDBOX_FILE_MAX_BYTES:
            raise ValueError(
                f"{vfs.render(scope, parent_parts + [leaf])} is over the "
                f"{SANDBOX_FILE_MAX_BYTES // 1_048_576} MB file limit."
            )
        if not _NAME.match(leaf) or leaf in ('.', '..'):
            raise ValueError(
                f"{leaf!r} cannot be handed to the sandbox — rename it to a "
                f"plain file name first."
            )
        if leaf in out:
            raise ValueError(
                f"Two inputs are both called {leaf!r} and would collide in the "
                f"sandbox. Rename one first."
            )
        out[leaf] = bytes(data)
    return out


def _save_outputs(scope, files_out: dict[str, bytes]) -> list[dict]:
    """Sandbox outputs -> workspace files. Sync (DB). Never overwrites."""
    from inference import vfs

    saved: list[dict] = []
    for name, data in (files_out or {}).items():
        if not _NAME.match(name) or name in ('.', '..'):
            continue
        # A bare name goes in the scope's own write folder (`/Chat/` in chat,
        # the agent's home for an agent) — the only place most scopes can
        # write, the same rule the office tools use.
        if '/' not in name.strip('/') and getattr(scope, 'write_prefix', None):
            save_path = '/' + '/'.join(scope.write_prefix) + '/' + name.strip('/')
        else:
            save_path = name
        parent_parts, leaf = vfs._split_leaf(scope, save_path)
        # Checked before creating parents: no litter from a refused write.
        vfs._require_write_at(scope, parent_parts, 'write')
        folder = vfs._make_dirs(scope, parent_parts)
        existing = vfs._document_in(scope, folder, leaf)
        if existing is not None:
            leaf = vfs._free_name(scope, folder, leaf)

        ext = leaf.rsplit('.', 1)[-1].lower() if '.' in leaf else ''
        if ext in vfs.BINARY_TYPES:
            result = vfs.write_binary(scope, vfs.render(scope, parent_parts + [leaf]),
                                      bytes(data), text='', overwrite=False)
        else:
            try:
                text = bytes(data).decode('utf-8')
            except UnicodeDecodeError:
                raise ValueError(
                    f"{name!r} is not text and has no binary extension. Save it "
                    f"with a binary extension (.xlsx, .png, …) instead."
                )
            # Same no-overwrite rule as binaries: `write_file` overwrites, so
            # resolve the free name first rather than destroying what is there.
            result = vfs.write_file(scope, vfs.render(scope, parent_parts + [leaf]), text)
        saved.append({
            "name": leaf,
            "path": result["path"],
            "document_id": result.get("document_id"),
            "bytes": len(data),
        })
    return saved
