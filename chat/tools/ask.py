"""
`ask_user`: an agent run's way to say "I need to know this", as a record.

An agent run had no way to ask. It could write a question into its final
answer — which ends the run — or guess. The eval grader for "asked instead of
assuming" had to find questions by scanning prose for words like "which", so an
answer that happened to contain "which" counted as asking.

This tool makes the question a call, so it is in the trace and in the run's
`intents` (`agents/agent/runtime.py::collect_intents`) whatever the answer's
wording. It never blocks: nobody is guaranteed to be watching an agent run, so
the reply tells the model to proceed on a stated assumption and to repeat the
question in its answer. The behaviour is identical in eval and in production,
which is the point — an eval of a tool that behaves differently under test
measures the test.

Agent runs only (`requires="agent_run"`, which chat never meets): in chat the
person is reading the reply, and a question there is just the reply.
"""
from __future__ import annotations

import json
from typing import Dict

from .registry import tool

QUESTION_CHARS = 500
ASSUMPTION_CHARS = 500


@tool({
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Record a question for the user when the task is ambiguous and the "
            "answer would change what you do — which of two things they meant, a "
            "value only they know, whether a risky step is wanted. Nobody can "
            "answer during this run: state the assumption you will proceed on, "
            "carry on with it, and repeat the question in your final answer. "
            "Do not ask what the task, its inputs or your tools already tell you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, as you would put it to the user.",
                },
                "assumption": {
                    "type": "string",
                    "description": "What you will assume and act on meanwhile.",
                },
            },
            "required": ["question", "assumption"],
        },
    },
}, requires="agent_run", effect="read")
async def ask_user(args: Dict, context: Dict) -> str:
    question = str(args.get("question") or "").strip()[:QUESTION_CHARS]
    assumption = str(args.get("assumption") or "").strip()[:ASSUMPTION_CHARS]
    if not question:
        return json.dumps({"error": "A question is required."})
    if not assumption:
        return json.dumps({
            "error": "State the assumption you will proceed on, then ask again.",
        })
    return json.dumps({
        "recorded": True,
        "answer": None,
        "note": (
            "No one can answer during this run. Proceed on your stated "
            f"assumption ({assumption}) and repeat the question in your final "
            "answer so the user can correct it."
        ),
    })
