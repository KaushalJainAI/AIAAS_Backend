"""
What the user's jobs are doing — for the orchestrator, not the dashboard.

`get_agent_run` answers about one run whose id the caller already holds.
This module is the other half: `list_user_runs` discovers them ("what is
running for this user?"), and `progress_for` compacts one run into a
model-sized block both tools share.

Compactness is the design, because the full trace already exists
(`logs/queries.py::execution_detail` serves `/runs`) and is the wrong shape
here: turns carry full reasoning and steps carry payloads, and handing that
to a model on every poll would cost thousands of tokens per call for a
question ("is it done?") worth dozens. So rows carry counts, short excerpts
and capped lists — never full reasoning, never step payloads.

One honest limit, stated in the schemas: `output_data` (todos, tasks, files)
is written at run *close*, so a running run's plan text is not on its row.
Live signals come from rows that *are* written live — turn/step counts, the
latest reasoning excerpt, in-process code-task buckets — plus a best-effort
read of the run's own checkpointer for its plan text. The block says which
source each half came from (`live`, `record`, or neither) rather than
blending them: a checkpoint read can miss (another process, another saver,
no state yet), and a miss degrades to turn activity instead of failing the
tool or inventing a plan.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from asgiref.sync import sync_to_async

from .registry import tool

from tools_config.overlay import alimit

logger = logging.getLogger(__name__)

#: Caps, all about keeping a status answer cheap. A row is a summary; the run
#: view behind `link` holds the trace.
_LIST_DEFAULT_LIMIT = 10
_LIST_MAX_LIMIT = 25
_GOAL_CHARS = 300
_REASONING_EXCERPT_CHARS = 500
_OPEN_TODO_ITEMS = 5
_OPEN_TODO_CHARS = 120
_TASK_ROWS = 8
_FILE_ROWS = 10


def _goal_of(log) -> str:
    """What the run was asked to do: the worker's task, else the run's goal."""
    return (log.delegation_task or (log.input_data or {}).get('goal') or '')[:_GOAL_CHARS]


def _todo_counts(items: Any) -> tuple[int, int, list[str]]:
    """`(done, total, open_texts)` over an `output_data['todos']` list."""
    if not isinstance(items, list):
        return 0, 0, []
    done = sum(1 for t in items
               if isinstance(t, dict) and t.get('status') == 'done')
    open_texts = [str(t.get('text') or '')[:_OPEN_TODO_CHARS]
                  for t in items
                  if isinstance(t, dict) and t.get('status') not in ('done', 'blocked')]
    return done, len(items), open_texts[:_OPEN_TODO_ITEMS]


def _task_rows(frames: Any) -> list[dict[str, str]]:
    """Compact `{title, status}` rows over task frames, whatever their source."""
    out = []
    if isinstance(frames, list):
        for frame in frames[:_TASK_ROWS]:
            if isinstance(frame, dict):
                out.append({
                    'title': str(frame.get('title') or frame.get('label') or '')[:_OPEN_TODO_CHARS],
                    'status': str(frame.get('status') or ''),
                })
    return out


def _file_names(entries: Any) -> list[str]:
    """File names out of an `output_data['files']` payload, capped."""
    if isinstance(entries, dict):
        entries = list(entries.values())
    if not isinstance(entries, list):
        return []
    names = []
    for entry in entries[:_FILE_ROWS]:
        if isinstance(entry, dict):
            name = entry.get('name') or entry.get('path') or ''
            if name:
                names.append(str(name))
    return names


def _live_task_frames(thread_id: str) -> list[dict[str, str]]:
    """Code-dispatch lanes still in this process, best-effort.

    Buckets live in `agents/agent/tasks.py` keyed by lead thread id and die
    with the process — a worker on another worker is simply absent, which is
    why this never fails and never claims completeness.
    """
    try:
        from agents.agent import tasks as _code_tasks

        bucket = _code_tasks._tasks.get(thread_id or '')
        if not bucket:
            return []
        return _task_rows([_code_tasks.task_frame(t) for t in bucket.values()])
    except Exception:  # noqa: BLE001 — live lanes are a bonus, not the report
        return []


#: Seconds per checkpoint read. The state holds the whole transcript, so a
#: long run's read is not free — and a tool that stalls stalls the turn.
_LIVE_TODO_TIMEOUT = 2.0
#: How many rows one listing spends checkpoint reads on. Running/paused lists
#: are short; beyond this the rest keep turn activity rather than each paying
#: seconds.
_LIVE_TODO_ROWS = 5


async def live_todos(thread_id: str) -> tuple[int, int, list[str]] | None:
    """This run's plan text, straight from its checkpointer — or None.

    Best-effort in three directions: no state yet, a saver this process
    cannot read, or a read slower than `_LIVE_TODO_TIMEOUT` all answer None,
    and the caller falls back to turn activity. None is honest ("nothing
    readable right now"), never an error, and never an invented plan.
    """
    if not thread_id:
        return None
    try:
        from chat.turn.agent import get_graph

        graph = get_graph()
        aget = getattr(graph, 'aget_state', None)
        if aget is None:
            return None
        state = await asyncio.wait_for(
            aget({'configurable': {'thread_id': thread_id}}),
            timeout=_LIVE_TODO_TIMEOUT)
        values = getattr(state, 'values', None) or {}
        items = (values.get('metadata') or {}).get('todos')
        done, total, open_items = _todo_counts(items)
        return (done, total, open_items) if total else None
    except Exception:  # noqa: BLE001 — see docstring
        return None


def progress_for(log, *, turns: int = 0, steps: int = 0,
                 last_reasoning: str = '',
                 live_todos: tuple[int, int, list[str]] | None = None,
                 ) -> Dict[str, Any]:
    """One run as a model-sized progress block. Pure — pass rows in.

    Counts arrive precomputed because relations cannot be touched from an
    async caller (`SynchronousOnlyOperation`); read them in the sync thread
    that fetched the row. `live_todos` is the checkpointer's plan when a
    live caller could read one; the run record stays the default because it
    is the source that is always readable. Either way the block names its
    source.
    """
    output = log.output_data or {}
    todos = output.get('todos')
    done, total, open_items = _todo_counts(todos)
    source = 'run record'
    if live_todos is not None:
        done, total, open_items = live_todos
        source = 'live'
    tasks = _task_rows(output.get('tasks')) or _live_task_frames(log.thread_id)
    block: Dict[str, Any] = {
        'status': log.status,
        'goal': _goal_of(log),
        'turns': turns,
        'steps': steps,
        'cost_usd': str(log.cost_usd or '0'),
        'link': '/runs',
    }
    if total:
        block['todos'] = {'done': done, 'total': total,
                          'open': open_items, 'source': source}
    elif last_reasoning:
        block['activity'] = {'latest': last_reasoning[:_REASONING_EXCERPT_CHARS],
                             'source': 'latest turn'}
    if tasks:
        block['tasks'] = tasks
    files = _file_names(output.get('files'))
    if files:
        block['files'] = files
    return block


def _row_summary(log, *, turns: int = 0, steps: int = 0,
                 last_reasoning: str = '',
                 needs_approval: bool = False,
                 live_todos: tuple[int, int, list[str]] | None = None,
                 ) -> Dict[str, Any]:
    """One list row: identity, status and progress, nothing more."""
    from django.utils import timezone

    started = log.started_at or log.created_at
    elapsed = (log.completed_at or timezone.now()) - started if started else None
    return {
        'execution_id': str(log.execution_id),
        'agent': (log.subagent.name if log.subagent_id and log.subagent else None),
        'status': log.status,
        'needs_approval': needs_approval,
        **progress_for(log, turns=turns, steps=steps,
                       last_reasoning=last_reasoning,
                       live_todos=live_todos),
        'started_at': started.isoformat() if started else None,
        'elapsed_seconds': int(elapsed.total_seconds()) if elapsed else 0,
    }


@tool({
    'type': 'function',
    'function': {
        'name': 'list_user_runs',
        'description': (
            'See what this user\u2019s jobs are doing — running first. One row '
            'per run with status, goal, turn/step counts, todo and task '
            'progress, cost and whether it is paused waiting on them. Use it '
            'when the user asks what is happening ("is my research done?", '
            '"what did you start for me?"), to resolve "that invoice job" to '
            'an id before calling get_agent_run, and before notifying about a '
            'completion — check first rather than announcing from memory. '
            'Closed runs carry their plan progress; running runs carry live '
            'activity instead, because plans are recorded at close.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'status': {
                    'type': 'string',
                    'description': 'running, paused, completed, failed, cancelled, timeout or all (default running).',
                },
                'limit': {
                    'type': 'integer',
                    'description': 'How many rows (default 10, max 25).',
                },
            },
            'additionalProperties': False,
        },
    },
}, parallel=True, effect='read')
async def list_user_runs(args: Dict, context: Dict) -> str:
    from agents.models import HITLRequest
    from logs.models import AgentStep, AgentTurn, ExecutionLog

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})

    wanted = str(args.get('status') or 'running').lower()
    valid = {'running', 'paused', 'completed', 'failed', 'cancelled',
             'timeout', 'all'}
    if wanted not in valid:
        return json.dumps({'error': f'Status must be one of {sorted(valid)}.'})

    try:
        limit = int(args.get('limit', _LIST_DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _LIST_DEFAULT_LIMIT
    cap = await alimit(context, 'list_user_runs', 'maxResults')
    limit = max(1, min(limit, _LIST_MAX_LIMIT, cap))

    def _read():
        qs = (ExecutionLog.objects
              .filter(user_id=user_id)
              .select_related('subagent')
              .order_by('-created_at'))
        if wanted == 'running':
            # The question is almost always "what is happening now": live
            # runs first, then ones paused for the user.
            qs = qs.filter(status__in=('running', 'paused'))
        elif wanted != 'all':
            qs = qs.filter(status=wanted)
        rows = list(qs[:limit])
        if not rows:
            return []
        ids = [r.id for r in rows]
        approvals = set(
            HITLRequest.objects
            .filter(execution_id__in=ids, status='pending')
            .values_list('execution_id', flat=True))
        out = []
        for log in rows:
            last = (AgentTurn.objects
                    .filter(execution=log).order_by('-index')
                    .values_list('reasoning', flat=True).first() or '')
            out.append((log,
                        AgentTurn.objects.filter(execution=log).count(),
                        AgentStep.objects.filter(execution=log).count(),
                        last, log.id in approvals))
        return out

    try:
        found = await sync_to_async(_read)()
    except Exception:  # noqa: BLE001
        logger.exception('[Runs] list failed')
        return json.dumps({'error': 'The runs could not be listed.'})
    if not found:
        scope = 'running or paused' if wanted == 'running' else wanted
        return json.dumps({'runs': [], 'count': 0,
                           'message': f'No {scope} runs for this user.'})
    # Live plan text for the runs still going, best-effort and bounded: a
    # checkpoint read can miss (another process, another saver, nothing
    # written yet), and a miss keeps turn activity rather than failing.
    live: dict[int, tuple[int, int, list[str]]] = {}
    attempts = 0
    for log, _, _, _, _ in found:
        if attempts >= _LIVE_TODO_ROWS:
            break
        if log.status not in ('running', 'paused') or not log.thread_id:
            continue
        attempts += 1
        read = await live_todos(log.thread_id)
        if read is not None:
            live[log.id] = read
    runs = [_row_summary(log, turns=turns, steps=steps,
                         last_reasoning=last,
                         needs_approval=approval,
                         live_todos=live.get(log.id))
            for log, turns, steps, last, approval in found]
    return json.dumps({'runs': runs, 'count': len(runs)})
