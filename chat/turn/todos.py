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
#:
#: The hard ceiling lives here (sync code cannot await the overlay); the
#: workspace knob (`update_todos.maxItems`) only ever narrows it — see
#: `normalize_for`. A knob above this is clamped down, never widened past it.
MAX_TODOS = 20

#: Characters per item. An item is a label, not a description — the reasoning
#: belongs in the turn the model is having, which `AgentTurn.reasoning` already
#: records.
MAX_TODO_CHARS = 200

#: Characters in an item's `note` — *why* it is blocked, or what it is waiting
#: on. A reason, not a result: the "intent, never results" rule still holds.
MAX_NOTE_CHARS = 200

#: Revisions of the plan one run keeps for the person watching
#: (`record_revision`). The first is always kept — it is the original plan,
#: the one every later revision should be read against.
MAX_REVISIONS = 30

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

    `owner` and `task_id` ride along untouched when present (the coding lead
    mirrors its plan this way: one todo per task). They are metadata for the
    panel, never rendered back into the model's context — `render` ignores
    them, so a chat without delegation reads exactly as before.
    """
    if not isinstance(raw, list):
        return []

    out: list[dict[str, str]] = []
    for entry in raw:
        if isinstance(entry, str):
            text, status = entry, OPEN
            owner, task_id, note = '', '', ''
        elif isinstance(entry, dict):
            text = entry.get('text') or entry.get('task') or entry.get('title') or ''
            status = (entry.get('status') or OPEN)
            owner = str(entry.get('owner') or '')[:80]
            task_id = str(entry.get('task_id') or entry.get('taskId') or '')[:64]
            note = str(entry.get('note') or entry.get('reason') or '').strip()[:MAX_NOTE_CHARS]
        else:
            continue

        text = str(text).strip()[:MAX_TODO_CHARS]
        if not text:
            continue

        status = str(status).strip().lower()
        if status not in STATUSES:
            status = OPEN
        item: dict[str, str] = {'text': text, 'status': status}
        if owner:
            item['owner'] = owner
        if task_id:
            item['task_id'] = task_id
        if note:
            item['note'] = note
        out.append(item)

        if len(out) >= MAX_TODOS:
            break
    return out


def normalize_for(raw: Any, limit: int) -> list[dict[str, str]]:
    """`normalize` with a caller-supplied cap, never wider than `MAX_TODOS`.

    The tool layer resolves the workspace knob and passes it in; sync callers
    without a user context keep calling `normalize`.
    """
    return normalize(raw)[:max(1, min(int(limit), MAX_TODOS))]


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
        why = f': {item["note"]}' if item.get('note') else ''
        lines.append(f'x {item["text"]} (blocked{why})')
    lines.append(
        'Keep this current with update_todos as you go. When everything is '
        'done or blocked, stop and answer.'
    )
    return '\n'.join(lines)


def missing_notes(todos: list[dict[str, str]]) -> list[str]:
    """Texts of blocked items that give no reason — the model is told to add one."""
    return [t['text'] for t in todos if t.get('status') == BLOCKED and not t.get('note')]


def current_step(todos: list[dict[str, str]]) -> str:
    """The first item in progress, or ''. Tool calls are filed under it."""
    for item in todos or []:
        if item.get('status') == DOING:
            return item.get('text') or ''
    return ''


def record_revision(meta: dict, todos: list[dict[str, str]]) -> int | None:
    """Append `todos` to `meta['todo_history']`; the new revision number, or None.

    The plan is replaced wholesale, so the current list alone cannot show what
    a revision *removed* — a step quietly dropped looks exactly like progress.
    The history is what lets the person watching see every change, and it
    lives in metadata for the reason the plan does (curation never touches
    it), then rides onto the saved message and `output_data`.

    An identical resend is not a revision. Past `MAX_REVISIONS` the oldest
    *intermediate* revision goes; the first — the original plan — never does.
    Revision numbers keep counting, so a gap says revisions were trimmed.
    """
    history = meta.setdefault('todo_history', [])
    if history and history[-1].get('todos') == todos:
        return None
    number = (history[-1].get('n', len(history)) + 1) if history else 1
    history.append({'n': number, 'todos': todos})
    if len(history) > MAX_REVISIONS:
        del history[1]
    return number
