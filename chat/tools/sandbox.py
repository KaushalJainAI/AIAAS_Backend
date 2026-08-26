"""
The Python sandbox tool: RestrictedPython inside wasmtime, no network, no
filesystem, no imports that reach either.
"""
from __future__ import annotations

import json
import logging

from typing import Any, Dict

from .registry import tool

logger = logging.getLogger(__name__)

#: How much sandbox stdout to hand back. A runaway loop printing megabytes would
#: otherwise blow the context window in a single tool call.
MAX_CODE_OUTPUT_CHARS = 20_000


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
            "description": "Run Python in a restricted sandbox and get its output back. No network, no filesystem, no imports beyond a safe standard subset (json, datetime, re, math, random, hashlib, base64, urllib.parse, itertools, functools, collections, string). Use it for arithmetic, data manipulation, parsing and simulation instead of computing in your head — you are unreliable at arithmetic and this is not. Assign the answer to a variable named `result`, or print it.",
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
    })
async def execute_python(args: Dict, context: Dict) -> str:
    """Run code through the same sandbox the Code node and agents use.

    Reusing `executor.sandbox` rather than adding a second sandbox is
    deliberate: a second one is a second thing to get wrong, and this one is
    already the audited path (docs/SANDBOX_EXECUTION.md). It is also why
    this is not the `execute_python_code` tool that was removed — that one
    ran `exec` against the process, and `chat/tests/test_rework.py` asserts it
    stays unreachable.

    Failures come back as a plain `Error: ...` string rather than JSON so a
    model reading the transcript cannot mistake a traceback for a result.
    """
    from asgiref.sync import sync_to_async
    from executor.sandbox.safe_execution import get_sandbox

    code = (args.get("code") or "").strip()
    if not code:
        return "Error: 'code' is required."

    # The sandbox joins a worker thread; keep the event loop free. Not
    # thread_sensitive: it touches no ORM and must not queue behind the
    # request's own executor.
    outcome = await sync_to_async(get_sandbox().execute, thread_sensitive=False)(code)

    if not outcome.get("success"):
        detail = outcome.get("error") or "Execution failed."
        if outcome.get("stderr"):
            detail = f"{detail}\n{outcome['stderr']}"
        return f"Error: {detail}"[:MAX_CODE_OUTPUT_CHARS]

    return json.dumps({
        "type": "code_execution",
        "result": _jsonable(outcome.get("result")),
        "stdout": (outcome.get("output") or "")[:MAX_CODE_OUTPUT_CHARS],
    })[:MAX_CODE_OUTPUT_CHARS]
