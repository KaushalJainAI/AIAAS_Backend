"""
What a run set out to do, kept where curation cannot reach it.

A long run forgets its own goal. `curate_node` replaces old tool results and
folds the oldest steps into a single running note, and the *first* thing it
folds is the beginning of the transcript — which is exactly where the original
instruction lives. What survives is a compressed trace of what happened, never
a statement of what was intended. Forty iterations in, the model is working
from a summary of its own footprints.

So the plan lives in `AgentState` as its own key rather than as a message.
Curation only ever rewrites `messages`, so a list held beside them is immune by
construction rather than by anyone remembering to exclude it. That is the whole
reason this is a state key and not, say, a system-message section or a pinned
`HumanMessage`.

Three rules the design leans on:

*The list holds intent; the transcript holds evidence.* Items never carry
results. The moment they do there are two transcripts, the second one worse and
also billed on every turn.

*The whole list is replaced, never patched.* `update_todos` takes the full set
each time. Add/complete/remove operations need stable ids the model has to
track across turns, and it will eventually address the wrong one — silently,
because nothing can tell a mistaken id from a deliberate one. Replacement is
idempotent and has no such failure.

*It is re-read, not just written.* A list nothing feeds back is theatre: the
model writes it, feels organised, and proceeds exactly as it would have. The
open items ride in the trailing context message on every turn, which is also
why they are capped — they are paid for every time.

Note the one thing that must **not** happen: this cannot go in the system
prompt. It changes on most turns, and the system prompt is the cached prefix.
That is the same trap the clock fell into (`prompts.build_context_update`), and
it costs the whole session's prefix caching, not just one call.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Items one run may track. A plan longer than this is not a plan; it is the
#: model narrating. Extra items are dropped from the end with a note, rather
#: than the write being refused, because a refused `update_todos` leaves the
#: previous list standing and the model believing it was replaced.
MAX_TODOS = 20

#: Characters per item. An item is a label, not a description — the reasoning
#: belongs in the turn the model is having, which `AgentTurn.reasoning` already
#: records.
MAX_TODO_CHARS = 200

OPEN = 'open'
DOING = 'doing'
DONE = 'done'
BLOCKED = 'blocked'

#: `blocked` exists so a run can finish honestly. Without it the only way to
#: end with an unfinished item is to mark it done, and a model that has been
#: told it may not stop while anything is open will do exactly that — which
#: turns the list from a record into a lie. See `unfinished`.
STATUSES = (OPEN, DOING, DONE, BLOCKED)

_TERMINAL = frozenset({DONE, BLOCKED})


def normalize(raw: Any) -> list[dict[str, str]]:
    """Coerce whatever the model sent into a list of `{text, status}`.

    Forgiving on shape and strict on vocabulary. A model that sends a bare
    string per item meant an open item and should get one; a model that invents
    a status has said something this system cannot act on, and quietly keeping
    it would let an item sit in a state nothing counts as either finished or
    outstanding.
    """
    if not isinstance(raw, list):
        return []

    out: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            text, status = entry, OPEN
        elif isinstance(entry, dict):
            text = entry.get('text') or entry.get('task') or entry.get('title') or ''
            status = (entry.get('status') or OPEN)
        else:
            continue

        text = str(text).strip()[:MAX_TODO_CHARS]
        if not text:
            continue

        status = str(status).strip().lower()
        if status not in STATUSES:
            status = OPEN
        out.append({'text': text, 'status': status})

        if len(out) >= MAX_TODOS:
            break
    return out


def unfinished(todos: list[dict[str, str]]) -> list[dict[str, str]]:
    """Items that are neither done nor explicitly blocked.

    `blocked` counts as settled on purpose. The question a caller asks with
    this is "did the run leave work silently undone", and an item the model
    marked blocked is not silent — it is a reported failure, which is the
    outcome worth encouraging over a false `done`.
    """
    return [t for t in todos if t.get('status') not in _TERMINAL]


def render(todos: list[dict[str, str]]) -> str:
    """The list as the model should see it on its next turn, or ''.

    Open work in full, finished work as a count. A model re-reading fifteen
    completed items every turn pays for them every turn and learns nothing it
    did not already know — while the count still tells it that progress is
    real, which is what stops it re-planning work it has already done.
    """
    if not todos:
        return ''

    done = sum(1 for t in todos if t.get('status') == DONE)
    blocked = [t for t in todos if t.get('status') == BLOCKED]
    live = [t for t in todos if t.get('status') in (OPEN, DOING)]

    if not live and not blocked:
        return f'### YOUR PLAN ###\nAll {done} steps are done. Give your final answer.'

    lines = ['### YOUR PLAN ###']
    if done:
        lines.append(f'{done} step(s) done already — do not repeat them.')
    for item in live:
        mark = '>' if item['status'] == DOING else '-'
        lines.append(f'{mark} {item["text"]}')
    for item in blocked:
        lines.append(f'x {item["text"]} (blocked)')
    lines.append(
        'Keep this current with update_todos as you go. When everything is '
        'done or blocked, stop and answer.'
    )
    return '\n'.join(lines)
