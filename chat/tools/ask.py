"""
`ask_user`: a structured question for the person — multiple choice, a number,
or a sentence — that pauses the run until it is answered.

Two lives, one tool, because a question is the same act wherever it is asked:

* **Someone can answer** (chat, a run started from chat or by hand, a worker
  whose manager is a model): `tools_node` stops on the call exactly as it stops
  for an approval — `interrupt()`, an `ask_question` frame for the chat card, a
  `clarification` row in the Inbox for an agent run — and on resume the tool is
  dispatched with the answer in its context. The model reads the answer as the
  tool's result. A manager answers its workers through `answer_subagent`, and
  may ask the person itself first; the human is the boss, the orchestrator the
  manager, the subagents the workhorses.
* **Nobody can** (a schedule, a trigger, an eval): the tool never blocks. It is
  recorded in the trace and in the run's `intents`
  (`agents/agent/runtime.py::collect_intents`) and tells the model to proceed
  on its stated assumption. An eval therefore measures the real behaviour —
  "did it ask" is a call, never a keyword scan of the answer.

The kinds are what the card can draw well: `choice` and `multi_choice` (2–8
options, optionally with "Other"), `number` (bounds, step, unit) and `text`.
A malformed spec is refused as an error the model can fix, never drawn as a
broken card — a question nobody can answer is worse than none.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict

from .registry import tool

QUESTION_CHARS = 500
ASSUMPTION_CHARS = 500
OPTION_CHARS = 120
ANSWER_CHARS = 2_000
MIN_OPTIONS, MAX_OPTIONS = 2, 8

KINDS = ('choice', 'multi_choice', 'number', 'text')

#: The tools `tools_node` pauses on as a *question* rather than an approval.
QUESTION_TOOLS = frozenset({'ask_user'})


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == '':
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def question_spec(args: Dict[str, Any] | None) -> tuple[dict[str, Any] | None, str]:
    """The question as the card draws it, or (None, why it cannot be asked)."""
    args = args or {}
    question = str(args.get('question') or '').strip()[:QUESTION_CHARS]
    if not question:
        return None, 'A question is required.'
    assumption = str(args.get('assumption') or '').strip()[:ASSUMPTION_CHARS]
    if not assumption:
        return None, 'State the assumption you will proceed on if nobody answers.'

    options: list[str] = []
    for raw in args.get('options') or []:
        text = str(raw or '').strip()[:OPTION_CHARS]
        if text and text not in options:
            options.append(text)

    kind = str(args.get('kind') or '').strip().lower()
    if kind not in KINDS:
        kind = 'choice' if options else 'text'

    spec: dict[str, Any] = {
        'question': question, 'assumption': assumption, 'kind': kind,
    }
    if kind in ('choice', 'multi_choice'):
        if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
            return None, (
                f'A {kind} question needs {MIN_OPTIONS}-{MAX_OPTIONS} distinct '
                f'options; it has {len(options)}.'
            )
        spec['options'] = options
        spec['allow_other'] = bool(args.get('allow_other'))
    elif kind == 'number':
        low, high, step = _num(args.get('min')), _num(args.get('max')), _num(args.get('step'))
        if low is not None and high is not None and low > high:
            return None, "'min' is greater than 'max'."
        if step is not None and step <= 0:
            return None, "'step' must be positive."
        spec.update(min=low, max=high, step=step,
                    unit=str(args.get('unit') or '').strip()[:24])
    return spec, ''


def normalise_answer(spec: dict[str, Any], answer: Any) -> tuple[Any, str]:
    """The answer checked against its question, or (None, why it does not fit).

    Checked on the server because the card is not the only door: the Inbox and
    an orchestrator answering its worker both arrive here too.
    """
    kind = spec.get('kind')
    options = spec.get('options') or []
    if kind == 'choice':
        value = str(answer if not isinstance(answer, list) else (answer[0] if answer else '')).strip()
        if not value:
            return None, 'Pick one option.'
        if value not in options and not spec.get('allow_other'):
            return None, f'{value!r} is not one of the options.'
        return value[:ANSWER_CHARS], ''
    if kind == 'multi_choice':
        values = answer if isinstance(answer, list) else [answer]
        picked = [str(v).strip()[:OPTION_CHARS] for v in values if str(v or '').strip()]
        if not picked:
            return None, 'Pick at least one option.'
        stray = [v for v in picked if v not in options]
        if stray and not spec.get('allow_other'):
            return None, f'{stray[0]!r} is not one of the options.'
        return picked, ''
    if kind == 'number':
        value = _num(answer)
        if value is None:
            return None, 'Enter a number.'
        if spec.get('min') is not None and value < spec['min']:
            return None, f"The number must be at least {spec['min']:g}."
        if spec.get('max') is not None and value > spec['max']:
            return None, f"The number must be at most {spec['max']:g}."
        return (int(value) if value.is_integer() else value), ''
    value = str(answer if answer is not None else '').strip()
    if not value:
        return None, 'Write an answer.'
    return value[:ANSWER_CHARS], ''


def render_answer(spec: dict[str, Any], value: Any) -> str:
    """The answer as a sentence the model reads."""
    if isinstance(value, list):
        return ', '.join(str(v) for v in value)
    if spec.get('kind') == 'number' and spec.get('unit'):
        return f'{value} {spec["unit"]}'
    return str(value)


@tool({
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the user one question when the task is genuinely ambiguous and "
            "the answer would change what you do — which of several things they "
            "meant, a number only they know (a budget, a count, a date range), "
            "whether a risky step is wanted. Prefer `choice` with 2-8 short "
            "options (add `allow_other` when your options may not cover it) or "
            "`number` with bounds; use `text` only when neither fits. The run "
            "pauses and the answer comes back as this tool's result. If nobody "
            "can answer (a scheduled run), you are told to proceed on your "
            "stated assumption — then repeat the question in your final answer. "
            "One question at a time, and never ask what the task, its inputs, "
            "the conversation or your tools already tell you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question, as you would put it to the user.",
                },
                "kind": {
                    "type": "string",
                    "enum": list(KINDS),
                    "description": (
                        "choice: pick one option; multi_choice: pick any; "
                        "number: a number within optional bounds; text: free text."
                    ),
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "2-8 short, distinct options (choice/multi_choice).",
                },
                "allow_other": {
                    "type": "boolean",
                    "description": "Also offer an 'Other' box for an answer not in the options.",
                },
                "min": {"type": "number", "description": "Lowest allowed number (number)."},
                "max": {"type": "number", "description": "Highest allowed number (number)."},
                "step": {"type": "number", "description": "Increment, e.g. 1 or 0.5 (number)."},
                "unit": {"type": "string", "description": "Unit shown beside the box, e.g. 'INR' or 'days' (number)."},
                "assumption": {
                    "type": "string",
                    "description": "What you will assume if the user skips the question or nobody can answer.",
                },
            },
            "required": ["question", "assumption"],
        },
    },
}, effect="read")
async def ask_user(args: Dict, context: Dict) -> str:
    spec, problem = question_spec(args)
    if spec is None:
        return json.dumps({"error": problem})

    if context.get("answered"):
        value, why = normalise_answer(spec, context.get("user_answer"))
        if value is None:
            # Validated at the door too; this is the backstop for a stored
            # answer that no longer fits (the question was re-asked differently).
            return json.dumps({
                "answered": False,
                "note": f"The answer did not fit the question ({why}). Proceed on "
                        f"your stated assumption ({spec['assumption']}).",
            })
        return json.dumps({
            "answered": True,
            "question": spec["question"],
            "answer": render_answer(spec, value),
            "value": value,
        })

    return json.dumps({
        "recorded": True,
        "answer": None,
        "note": (
            "No one can answer during this run. Proceed on your stated "
            f"assumption ({spec['assumption']}) and repeat the question in your "
            "final answer so the user can correct it."
        ),
    })
