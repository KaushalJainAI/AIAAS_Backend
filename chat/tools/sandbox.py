"""
The Python sandbox tool. In production the code runs in a hardened sidecar
container (`sandbox_service/`) — real kernel-level confinement, no network
egress, no secrets, and C extensions (numpy/pandas) available. Local dev with
no sidecar falls back to an in-process AST-guarded engine, which is weaker and
for development only. Engine selection lives in `sandbox/engine.py`.
"""
from __future__ import annotations

import json
import logging

from typing import Any, Dict

from .registry import tool

logger = logging.getLogger(__name__)

#: How much sandbox stdout to hand back when the user has not said otherwise.
#: A runaway loop printing megabytes would blow the context window in a single
#: tool call. The user-facing knob is `tools_config.settings_schema`, whose
#: default is this number; this stays as the floor under a failed read.
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
            "description": "Run Python in a sandbox and get its output back. No network access. The standard library is available, plus numpy and pandas for numeric and tabular work. Use it for arithmetic, data manipulation, parsing, analysis and simulation instead of computing in your head — you are unreliable at arithmetic and this is not. Assign the answer to a variable named `result`, or print it.",
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
