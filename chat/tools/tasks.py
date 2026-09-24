"""
Detached coding tasks: what the coding lead calls instead of `invoke_subagent`.

`invoke_subagent` blocks until every worker finishes, so a lead that uses it
has no turn in which to steer or stop anyone. These six tools split starting
from waiting: `start_tasks` returns handles immediately, and `wait_tasks`
returns on the first *event* (a finish, a failure, a pause for approval, or a
refusal), the pattern P7's `wait_for` uses for missions.

Offered only with the `subAgents` grant, like the delegation tools — and
workers never hold that grant, so depth stays 1 for code: a debugger that
wants help says so in its `patch` followups and the lead decides.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from .registry import tool

from workflow_backend.background import release_db

logger = logging.getLogger(__name__)

#: Tools that write a project file. A task whose worker can reach any of
#: these must carry non-empty `claims` — otherwise there is nothing to lease
#: and nothing to sequence on.
_WRITING_TOOLS = ('ws_write', 'ws_edit', 'ws_apply_patch')


def _task_list(args: Dict) -> list[dict]:
    tasks = args.get('tasks')
    if isinstance(tasks, dict):
        tasks = [tasks]
    if not isinstance(tasks, list):
        return []
    return [t for t in tasks if isinstance(t, dict)]


async def _resolve_worker(args_agent: Any, user, delegation_scope) -> tuple[Any, str]:
    """The saved agent a task names, or an error written for the lead.

    Accepts a numeric id (checked against the lead's `delegatesTo`, like
    `invoke_subagent`), a template slug (`code-implementer`) resolving to the
    caller's installed row, or a name. A missing row is refused with the fix —
    install the code pack — not with a bare "not found".
    """
    from agents.models import SubAgent

    if args_agent is None or (isinstance(args_agent, str) and not args_agent.strip()):
        return None, (
            'Give the `agent` for each task: a saved agent id, its template '
            'slug (e.g. "code-implementer"), or its name.')
    if isinstance(args_agent, int) or (isinstance(args_agent, str) and args_agent.strip().isdigit()):
        agent_id = int(args_agent)
        if delegation_scope is not None and agent_id not in delegation_scope:
            return None, (
                f'Agent {agent_id} is not one this lead may delegate to. '
                f'Call search_agents to see the ones it can.')
        row = await SubAgent.objects.filter(id=agent_id, user_id=user.id).afirst()
        if row is None:
            return None, f'No agent {agent_id} belongs to this user.'
        return row, ''
    slug = str(args_agent).strip()
    row = await SubAgent.objects.filter(
        user_id=user.id, template_slug=slug).afirst()
    if row is None:
        row = await SubAgent.objects.filter(
            user_id=user.id, name__iexact=slug).afirst()
    if row is None:
        return None, (
            f'No agent "{slug}" is installed. Install the code pack '
            f'(template {slug}) or pass a saved agent id from search_agents.')
    if delegation_scope is not None and row.id not in delegation_scope:
        return None, (
            f'Agent "{slug}" is not one this lead may delegate to. '
            f'Call search_agents to see the ones it can.')
    return row, ''


async def _resolve_project(args_project: Any, context: Dict, user) -> tuple[Any, str]:
    """The `CodeProject` a task runs in, or an error written for the lead."""
    from workspaces.models import CodeProject

    name = str(args_project or context.get('code_project') or '').strip()
    if not name:
        # The lead's own scope when it names exactly one project; otherwise
        # the task must say which.
        scope = context.get('code_projects')
        if isinstance(scope, (list, tuple)) and len(scope) == 1:
            row = await CodeProject.objects.filter(
                user_id=user.id, id=scope[0]).afirst()
            if row is not None:
                return row, ''
        return None, 'Give the `project` for each task.'
    row = await CodeProject.objects.filter(user_id=user.id, name=name).afirst()
    if row is None:
        return None, f'No project {name!r} belongs to this user.'
    scope = context.get('code_projects')
    if scope is not None and row.id not in scope:
        return None, f'Project {name!r} is not selected for this run.'
    return row, ''


def _normalise_claims(raw: Any) -> list[str]:
    from workspaces.leases import normalize_pattern

    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [p for p in (normalize_pattern(p) for p in raw) if p]


def _can_write(worker) -> bool:
    """Whether the worker's toolbox can write a project file."""
    from agents.agent.runtime import GRANT_TOOLS, tool_scope_for

    scope = tool_scope_for(worker)
    # `toolScope` is the older axis: empty means everything its grants unlock.
    effective = set(scope) if scope else set(GRANT_TOOLS.get('shell', ()))
    return any(t in effective for t in _WRITING_TOOLS)


@tool({
    'type': 'function',
    'function': {
        'name': 'start_tasks',
        'description': (
            'Start coding tasks as detached workers and return their handles '
            'immediately. Each task names `agent` (a saved id, template slug '
            'or name), `instructions`, `claims` (file globs it will write — '
            'required when its agent can write), `reads`, `depends_on` '
            '(earlier task ids) and `project`. Ready tasks start now; tasks '
            'whose dependencies are unfinished are refused, naming them; '
            'tasks whose claims overlap each other or a live lease are '
            'refused, naming the holder. At most 3 workers run at once. '
            'Follow with wait_tasks to hear back.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'tasks': {
                    'type': 'array',
                    'items': {'type': 'object'},
                    'description': '`code_plan` task objects to start.',
                },
                'briefing': {
                    'type': 'string',
                    'description': 'Shared background (the plan goal) sent to every worker once.',
                },
            },
            'required': ['tasks'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='reversible')
async def start_tasks(args: Dict, context: Dict) -> str:
    from django.contrib.auth import get_user_model

    from agents.agent import tasks as _tasks
    from agents.agent.orchestrator import check_depth, DelegationRefused

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    depth = int(context.get('depth', 0) or 0)
    try:
        check_depth(depth)
    except DelegationRefused as exc:
        return json.dumps({'error': str(exc), 'refused': True})

    items = _task_list(args)
    if not items:
        return json.dumps({'error': "Give `tasks` as a non-empty list of task objects."})
    if len(items) > _tasks.MAX_TASKS_PER_CALL:
        return json.dumps({'error': (
            f'{len(items)} tasks is more than the {_tasks.MAX_TASKS_PER_CALL} one '
            f'start_tasks call may carry. Split the plan and start what is ready.')})

    user = await get_user_model().objects.filter(id=user_id).afirst()
    if user is None:
        return json.dumps({'error': 'User not found.'})

    parent_thread = str(context.get('session_id') or 'run')
    bucket = _tasks._bucket(parent_thread)
    done_ids = {t.task_id for t in bucket.values() if t.status == 'done'}
    briefing = str(args.get('briefing') or '').strip()
    parent_paths = context.get('write_paths')
    parent_commands = context.get('command_scope')
    # Whoever is watching the lead sees worker frames live from here on.
    _tasks.set_sink(parent_thread, context.get('sink'))

    started: list[dict] = []
    refused: list[dict] = []
    # Claims taken by tasks started *in this call*, so two siblings claiming
    # the same file cannot both start: the second is refused with the first
    # named, exactly as a live lease refusal reads.
    batch_claims: list[tuple[str, str, str]] = []

    for index, item in enumerate(items):
        task_id = str(item.get('id') or item.get('task_id') or f't{index + 1}')
        title = str(item.get('title') or task_id)[:200]
        # Dependencies unfinished → refused, naming them. The lead waits and
        # starts these when `wait_tasks` reports their blockers done.
        deps = item.get('depends_on') or item.get('dependsOn') or []
        if isinstance(deps, str):
            deps = [deps]
        missing = [str(d) for d in deps
                   if str(d) and str(d) not in done_ids]
        if missing:
            refused.append({'task_id': task_id, 'title': title,
                            'refused': f'Dependencies unfinished: {", ".join(missing)}. '
                                       f'Start these after wait_tasks reports them done.'})
            continue
        if _tasks.live_count(parent_thread) >= _tasks.MAX_CODE_WORKERS:
            refused.append({'task_id': task_id, 'title': title,
                            'refused': (
                                f'{_tasks.MAX_CODE_WORKERS} workers are already running; '
                                f'wait for one with wait_tasks, then start this.')})
            continue

        worker, error = await _resolve_worker(
            item.get('agent'), user, context.get('delegation_scope'))
        if worker is None:
            refused.append({'task_id': task_id, 'title': title, 'refused': error})
            continue
        project, error = await _resolve_project(
            item.get('project'), context, user)
        if project is None:
            refused.append({'task_id': task_id, 'title': title, 'refused': error})
            continue
        claims = _normalise_claims(item.get('claims'))
        if _can_write(worker) and not claims:
            refused.append({'task_id': task_id, 'title': title,
                            'refused': (
                                f'Task {task_id} has no `claims` but its agent '
                                f'({worker.name}) can write. Name the file globs it '
                                f'will write so they can be leased and sequenced.')})
            continue
        # Overlap inside this batch, before anything starts.
        clash = _batch_clash(claims, batch_claims)
        if clash:
            other_task, other_pattern = clash
            refused.append({'task_id': task_id, 'title': title,
                            'refused': (
                                f'Claims {claims} overlap task {other_task}\'s '
                                f'claim on {other_pattern} started in this same call; '
                                f'sequence them through depends_on.')})
            continue

        outcome = await _launch_one(
            parent_thread=parent_thread, bucket=bucket, task_id=task_id,
            title=title, item=item, worker=worker, project=project,
            claims=claims, briefing=briefing, context=context, user=user,
            depth=depth, parent_paths=parent_paths,
            parent_commands=parent_commands,
            share_index=len(started), share_total=len(items))
        if outcome.get('handle'):
            started.append(outcome)
            batch_claims.extend((c, task_id, outcome['handle']) for c in claims)
        else:
            refused.append({'task_id': task_id, 'title': title,
                            'refused': outcome.get('error', 'Could not start.')})

    return json.dumps({'started': started, 'refused': refused})


def _batch_clash(claims: list[str],
                 batch: list[tuple[str, str, str]]) -> tuple[str, str] | None:
    """`(task_id, pattern)` of the first batch claim overlapping `claims`."""
    from workspaces.leases import overlaps

    for claim in claims:
        for pattern, task_id, _handle in batch:
            if overlaps(pattern, claim):
                return task_id, pattern
    return None


async def _launch_one(*, parent_thread, bucket, task_id, title, item, worker,
                      project, claims, briefing, context, user, depth,
                      parent_paths, parent_commands,
                      share_index, share_total) -> dict:
    """Open the worker's log, lease its claims, spawn its run. One task."""
    from asgiref.sync import sync_to_async

    from agents import budget
    from agents.agent import tasks as _tasks
    from agents.agent.orchestrator import divide_budget, worker_grants, worker_thread_id
    from agents.agent.runtime import (
        command_scope_for,
        intersect_command_scope,
        intersect_write_paths,
        run_agent,
        write_paths_for,
    )
    from workspaces import leases as _leases
    from workflow_backend.background import spawn

    # Narrowed toolbox: never the parent's, minus the right to delegate —
    # workers are depth+1 and cannot start tasks of their own.
    worker.tool_grants = worker_grants(worker.tool_grants or {})
    parent_permissions = context.get('tool_permissions') or {}
    if isinstance(parent_permissions, dict) and parent_permissions:
        from agents.agent.runtime import merge_tool_permissions, tool_permissions_for

        merged = merge_tool_permissions(
            parent_permissions, tool_permissions_for(worker))
        if merged != tool_permissions_for(worker):
            worker.agent_context = dict(
                worker.agent_context or {}, toolPermissions=merged)
    narrowed_paths = intersect_write_paths(parent_paths, write_paths_for(worker))
    narrowed_commands = intersect_command_scope(parent_commands, command_scope_for(worker))
    # The task's claims narrow the write set further: an implementer may only
    # write what its task claimed, even if its template allows more.
    effective_paths = _intersect_claims(narrowed_paths, claims)

    # The spend share is reserved before the first worker starts: sibling
    # workers all read "under the cap" otherwise, and all proceed on it.
    cap = (worker.guardrails or {}).get('spendCapRupees')
    if cap:
        worker.guardrails = dict(worker.guardrails or {},
                                 spendCapRupees=divide_budget(cap, max(1, share_total)))

    parent_deadline = context.get('deadline')
    worker_deadline = None
    if parent_deadline is not None:
        try:
            worker_deadline = parent_deadline.child(budget.limit_for(worker))
        except budget.OutOfTime as exc:
            return {'error': str(exc)}

    handle = _tasks.make_handle(task_id, bucket)
    label = _tasks.make_label(worker.name, bucket)
    thread_id = _tasks.new_thread_id(parent_thread)
    instructions = str(item.get('instructions') or item.get('title') or title)
    if len(instructions) > 8000:
        return {'error': (
            f'Task {task_id} is {len(instructions):,} characters, over the 8,000 '
            f'limit. Put shared background in `briefing` and keep each task to '
            f'what that worker alone must do.')}

    # The log first, so the execution id exists before the run does anything —
    # and so the leases have a holder to name.
    from agents.agent.runtime import _open_log

    try:
        log = await _open_log(
            worker, user, instructions, 'api', thread_id,
            caller='orchestrator', depth=depth + 1,
            parent_step_id=await _parent_step(context),
            delegation_task=instructions, delegation_index=share_index,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception('[Tasks] Could not open log for task %s', task_id)
        return {'error': f'Could not open the run: {exc}'}

    # Leases before the spawn, atomically: a task whose claims overlap a live
    # lease does not start, and the lead is told who holds it.
    try:
        def _take():
            return _leases.acquire(
                project, log, claims, holder_label=label, task_id=task_id)

        if claims:
            await sync_to_async(_take)()
    except Exception as exc:  # noqa: BLE001 — LeaseConflict names the holder
        await sync_to_async(log.delete)()
        return {'error': _lease_reason(exc)}

    # Who is asking, for the approval rows this worker may open: the Inbox
    # reads "Implementer #2 (task t3: add retry) wants to run …" from here
    # rather than a bare tool call.
    try:
        def _stamp():
            log.input_data = {
                **(log.input_data or {}),
                'worker_label': label,
                'task_id': task_id,
                'task_title': title,
            }
            log.save(update_fields=['input_data', 'updated_at'])

        await sync_to_async(_stamp)()
    except Exception:  # noqa: BLE001 — the label is courtesy, not record
        pass

    record = _tasks.CodeTask(
        handle=handle, task_id=task_id, title=title,
        agent_id=worker.id, agent_name=worker.name, label=label,
        project_id=project.id, project_name=project.name,
        claims=tuple(claims), thread_id=thread_id,
        execution_id=str(log.execution_id))
    bucket[handle] = record
    # The lane appears in the panel now, not when the first wait lands.
    await _emit_task(parent_thread, record)
    await _emit_leases(parent_thread, project.id)

    parent_scope = context.get('file_scope')
    workspace = tuple(getattr(parent_scope, 'write_prefix', None) or ())

    async def _run() -> None:
        try:
            run = await run_agent(
                worker, instructions, user=user, thread_id=thread_id,
                trigger_type='api', caller='orchestrator', depth=depth + 1,
                log=log,
                parent_step_id=await _parent_step(context),
                delegation_task=instructions, delegation_index=share_index,
                deadline=worker_deadline,
                briefing=briefing or str(item.get('goal') or ''),
                parent_session_key=str(context.get('session_id') or ''),
                workspace=workspace,
                task_claims=tuple(claims), task_id=task_id, worker_label=label,
                write_paths=effective_paths, command_scope=narrowed_commands,
            )
            answer = await _bound_answer(run.answer or '', context, handle)
            _tasks.finish(record, status='paused' if run.awaiting_approval else 'done',
                          answer=answer, tokens=run.tokens)
        except asyncio.CancelledError:
            # `cancel_agent_run` closes the log; the record just marks it.
            _tasks.finish(record, status='cancelled', error='Stopped.')
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception('[Tasks] Worker %s failed', handle)
            _tasks.finish(record, status='failed', error=str(exc))
        finally:
            # Terminal state always arrives (no throttle): the panel must not
            # need a wait to learn a worker ended, and the lock list clears
            # with the leases `_close_log` just released.
            await _emit_task(parent_thread, record, force=True)
            if record.project_id:
                await _emit_leases(parent_thread, record.project_id)

    spawn(_run(), name=f'code-task:{record.execution_id}')
    return {'handle': handle, 'task_id': task_id, 'title': title,
            'agent': worker.name, 'label': label,
            'execution_id': record.execution_id, 'claims': claims}


def _intersect_claims(narrowed: tuple[str, ...] | None,
                      claims: list[str]) -> tuple[str, ...] | None:
    """The worker's write set: its narrowed `writePaths` ∩ the task's claims.

    No claims means the template scope stands on its own (a reviewer reads; it
    simply never writes). Otherwise only claims the scope already covers are
    kept, glob-aware — a claim outside the scope would widen the worker past
    its lead, so it is dropped here and the write is refused as outside
    `writePaths` with its reason. Both axes stay enforced at every write.
    """
    if not claims:
        return narrowed
    if narrowed is None:
        return tuple(claims)
    if not narrowed:
        return ()
    from workspaces.leases import subsumes

    return tuple(c for c in claims if any(subsumes(p, c) for p in narrowed))


def _lease_reason(exc: BaseException) -> str:
    from workspaces.leases import LeaseConflict

    if isinstance(exc, LeaseConflict):
        return str(exc)
    cause = getattr(exc, '__cause__', None)
    if isinstance(cause, LeaseConflict):
        return str(cause)
    text = str(exc)
    if 'overlap' in text and 'lease' in text:
        return text
    return f'Could not take the file leases: {text}'


async def _emit_task(parent_thread: str, record, *, force: bool = False) -> None:
    """One `task_update` frame to whoever is watching the lead."""
    from agents.agent import tasks as _tasks
    from chat.turn.events import Event

    await _tasks.emit(
        parent_thread, Event.TASK_UPDATE, _tasks.task_frame(record),
        throttle_key=f'{parent_thread}:{record.handle}',
        throttle_seconds=0.0 if force else _tasks.TASK_UPDATE_THROTTLE_SECONDS)


async def _emit_leases(parent_thread: str, project_id: int) -> None:
    """The current `lease_update` frame for one project."""
    from asgiref.sync import sync_to_async

    from agents.agent import tasks as _tasks
    from chat.turn.events import Event
    from workspaces.models import CodeLease

    try:
        rows = await sync_to_async(list)(CodeLease.objects.filter(
            project_id=project_id).order_by('acquired_at').values(
                'pattern', 'holder_label', 'task_id'))
    except Exception:  # noqa: BLE001
        return
    await _tasks.emit(parent_thread, Event.LEASE_UPDATE, {
        'project': project_id,
        'leases': [{'pattern': r['pattern'],
                    'holder_label': r['holder_label'] or '',
                    'task_id': r['task_id'] or ''} for r in rows],
    })


async def _parent_step(context: Dict):
    from chat.tools.agents import _parent_step_id

    try:
        return await _parent_step_id(context)
    except Exception:  # noqa: BLE001
        return None


async def _bound_answer(answer: str, context: Dict, handle: str) -> str:
    """Cap a worker's answer the way fan-out results are capped, archiving the
    full text so `read_tool_output` can fetch it. What is cut stays reachable."""
    from agents.agent.orchestrator import WORKER_ANSWER_CHAR_LIMIT

    if len(answer) <= WORKER_ANSWER_CHAR_LIMIT:
        return answer
    full = answer
    try:
        from chat.tools.tool_output import spill

        output_id = await spill(f'task:{handle}', full, context)
    except Exception:  # noqa: BLE001
        output_id = None
    if output_id is None:
        return full[:WORKER_ANSWER_CHAR_LIMIT] + (
            '\n\n[trimmed to %d characters; the rest could not be kept]'
            % WORKER_ANSWER_CHAR_LIMIT)
    return full[:WORKER_ANSWER_CHAR_LIMIT] + (
        '\n\n[trimmed to %d characters. The full answer is stored — call '
        'read_tool_output with id "%s" to read the rest.]'
        % (WORKER_ANSWER_CHAR_LIMIT, output_id))


@tool({
    'type': 'function',
    'function': {
        'name': 'wait_tasks',
        'description': (
            'Wait until something happens on the given tasks (or all of them): '
            'a task finished, failed, or paused for approval. Returns one line '
            'per event plus progress for the rest. Also returns on timeout '
            '(at most 300s), reporting progress. Call in a loop: update your '
            'todos from the events, start newly-ready tasks, steer or stop '
            'the ones going wrong.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'handles': {
                    'type': 'array', 'items': {'type': 'string'},
                    'description': 'Handles from start_tasks. Omit for all of this run\'s tasks.',
                },
                'until': {
                    'type': 'string',
                    'description': '`any` (the first event) or `all` (every task done, failed or paused).',
                },
                'timeout_s': {
                    'type': 'integer',
                    'description': 'Seconds to wait, at most 300.',
                },
            },
            'additionalProperties': False,
        },
    },
}, effect='read')
async def wait_tasks(args: Dict, context: Dict) -> str:
    from agents.agent import tasks as _tasks

    parent_thread = str(context.get('session_id') or 'run')
    bucket = _tasks._bucket(parent_thread)
    wanted = [str(h) for h in (args.get('handles') or []) if str(h).strip()]
    _tasks.set_sink(parent_thread, context.get('sink'))
    if wanted:
        unknown = [h for h in wanted if h not in bucket]
        if unknown:
            return json.dumps({'error': (
                f'Unknown task handles: {", ".join(unknown)}. '
                f'Yours are: {", ".join(sorted(bucket)) or "none"}.')})
        records = [bucket[h] for h in wanted]
    else:
        records = list(bucket.values())
    if not records:
        return json.dumps({'events': [], 'progress': 'No tasks have been started.'})

    until = str(args.get('until') or 'any').strip().lower()
    if until not in ('any', 'all'):
        return json.dumps({'error': '`until` is "any" or "all".'})
    try:
        timeout = max(1, min(int(args.get('timeout_s') or 60), 300))
    except (TypeError, ValueError):
        return json.dumps({'error': '`timeout_s` must be a number.'})

    _tasks.prune(parent_thread)
    try:
        async with asyncio.timeout(timeout):
            while True:
                await _refresh_statuses(records)
                for r in records:
                    # Lanes move while the lead waits: throttled to one frame
                    # per second per task, so waiting does not flood the panel.
                    await _emit_task(parent_thread, r)
                events = _collect_events(records)
                # File changes touching another task's reads (C3): the lead
                # may re-steer a worker whose target just changed shape.
                events.extend(_tasks.drain_events(parent_thread))
                if until == 'any' and events:
                    return json.dumps(_report(records, events))
                if until == 'all' and all(r.done.is_set() for r in records):
                    return json.dumps(_report(records, events or _collect_events(records)))
                if all(r.done.is_set() for r in records):
                    return json.dumps(_report(records, _collect_events(records)))
                # The wait below is on worker events, not the database. Hand
                # the lead's pooled connection back each lap, or a lead waiting
                # on workers pins one for the whole delegation (G1). The status
                # refresh above re-takes one per lap in microseconds.
                await release_db()
                try:
                    await asyncio.wait(
                        [asyncio.ensure_future(r.done.wait()) for r in records
                         if not r.done.is_set()],
                        timeout=_tasks.WAIT_POLL_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
    except (asyncio.TimeoutError, TimeoutError):
        await _refresh_statuses(records)
        events = _collect_events(records)
        events.extend(_tasks.drain_events(parent_thread))
        lines = [f'{r.handle} ({r.title}): still {r.status}' for r in records
                 if not r.done.is_set()]
        return json.dumps(_report(records, events, note=(
            f'Timed out after {timeout}s. ' + (' '.join(lines) or 'Everything finished.'))))


async def _refresh_statuses(records: list) -> None:
    """Mark records paused/completed from their worker rows.

    A worker paused for approval has no live task — `interrupt()` returned —
    so its record would read `running` for ever without this: the row is the
    only thing that knows. Terminal statuses arrive through `finish` in the
    wrapper; this only ever moves `running` → `paused`.
    """
    from asgiref.sync import sync_to_async

    from logs.models import ExecutionLog

    for record in records:
        if record.done.is_set() or not record.execution_id:
            continue
        try:
            status = await sync_to_async(ExecutionLog.objects.filter(
                execution_id=record.execution_id
            ).values_list('status', flat=True).first)()
        except Exception:  # noqa: BLE001
            continue
        if status == 'paused':
            record.status = 'paused'


def _collect_events(records: list) -> list[str]:
    events = []
    for r in records:
        if r.status == 'done':
            events.append(f'{r.handle} ({r.title}): done by {r.label}'
                          + (f' — {r.answer[-500:]}' if r.answer else ''))
        elif r.status == 'failed':
            events.append(f'{r.handle} ({r.title}): failed — {r.error[:500]}')
        elif r.status == 'paused':
            events.append(f'{r.handle} ({r.title}): paused for approval — '
                          f'answer it in the Inbox, then steer or wait again.')
        elif r.status == 'cancelled':
            events.append(f'{r.handle} ({r.title}): stopped.')
    return events


def _report(records: list, events: list[str], note: str = '') -> dict:
    progress = ' '.join(
        f'{r.handle}:{r.status}' for r in records) or 'No tasks.'
    out: dict[str, Any] = {'events': events, 'progress': progress}
    if note:
        out['note'] = note
    return out


@tool({
    'type': 'function',
    'function': {
        'name': 'task_status',
        'description': (
            'A snapshot of one task (or all): status, worker, leases held, '
            'spend so far, and the result or error when finished.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'handle': {'type': 'string', 'description': 'Handle from start_tasks. Omit for all.'},
            },
            'additionalProperties': False,
        },
    },
}, effect='read', parallel=True)
async def task_status(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async

    from agents.agent import tasks as _tasks
    from logs.models import ExecutionLog
    from workspaces.models import CodeLease

    parent_thread = str(context.get('session_id') or 'run')
    bucket = _tasks._bucket(parent_thread)
    handle = str(args.get('handle') or '').strip()
    if handle:
        record = bucket.get(handle)
        if record is None:
            return json.dumps({'error': f'Unknown task handle {handle!r}.'})
        records = [record]
    else:
        records = list(bucket.values())

    out = []
    for r in records:
        tokens = r.tokens
        status = r.status
        try:
            row = await sync_to_async(ExecutionLog.objects.filter(
                execution_id=r.execution_id).values(
                    'status', 'tokens_used').first)()
            if row:
                status = r.status if r.done.is_set() else row['status']
                tokens = row['tokens_used'] or tokens
        except Exception:  # noqa: BLE001
            pass
        try:
            leases = await sync_to_async(list)(CodeLease.objects.filter(
                holder__execution_id=r.execution_id
            ).values_list('pattern', flat=True))
        except Exception:  # noqa: BLE001
            leases = []
        out.append({
            'handle': r.handle, 'task_id': r.task_id, 'title': r.title,
            'agent': r.agent_name, 'label': r.label, 'status': status,
            'execution_id': r.execution_id, 'claims': list(r.claims),
            'leases': list(leases), 'tokens': tokens,
            'answer_tail': r.answer[-1000:] if r.answer else '',
            'error': r.error[:500] if r.error else '',
        })
    return json.dumps({'tasks': out})


@tool({
    'type': 'function',
    'function': {
        'name': 'steer_task',
        'description': (
            'Send an instruction to a running worker. It lands at its next '
            'tool boundary, like steering a chat turn mid-run. Same queue and '
            'caps as a user steer.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'handle': {'type': 'string'},
                'message': {'type': 'string'},
            },
            'required': ['handle', 'message'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def steer_task(args: Dict, context: Dict) -> str:
    from agents.agent import tasks as _tasks
    from chat.turn import steering

    parent_thread = str(context.get('session_id') or 'run')
    record = _tasks.get(parent_thread, str(args.get('handle') or ''))
    if record is None:
        return json.dumps({'error': f'Unknown task handle {args.get("handle")!r}.'})
    if record.done.is_set():
        return json.dumps({'error': f'{record.handle} already finished ({record.status}).'})
    message = str(args.get('message') or '').strip()
    if not message:
        return json.dumps({'error': 'Give the message to send.'})
    steering.post(record.thread_id, message)
    stats = steering.stats(record.thread_id)
    return json.dumps({'steered': record.handle, **stats})


@tool({
    'type': 'function',
    'function': {
        'name': 'stop_task',
        'description': (
            'Stop a worker mid-task. Its leases are released and its file '
            'changes stay for the lead to keep or revert with revert_task.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'handle': {'type': 'string'},
                'reason': {'type': 'string'},
            },
            'required': ['handle'],
            'additionalProperties': False,
        },
    },
}, effect='reversible')
async def stop_task(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async

    from agents.agent import tasks as _tasks
    from logs.models import ExecutionLog

    parent_thread = str(context.get('session_id') or 'run')
    record = _tasks.get(parent_thread, str(args.get('handle') or ''))
    if record is None:
        return json.dumps({'error': f'Unknown task handle {args.get("handle")!r}.'})
    if record.done.is_set():
        return json.dumps({'error': f'{record.handle} already finished ({record.status}).'})
    try:
        log = await sync_to_async(ExecutionLog.objects.filter(
            execution_id=record.execution_id).first)()
    except Exception:  # noqa: BLE001
        log = None
    if log is None:
        _tasks.finish(record, status='cancelled', error='Run record gone.')
        return json.dumps({'stopped': record.handle, 'status': 'cancelled'})
    try:
        status = await _cancel_detached(log)
    except Exception as exc:  # noqa: BLE001 — names the fix
        return json.dumps({'error': str(exc)})
    _tasks.finish(record, status='cancelled',
                  error=str(args.get('reason') or 'Stopped by the lead.'))
    _tasks.prune(parent_thread)
    return json.dumps({'stopped': record.handle, 'status': status})


async def _cancel_detached(log) -> str:
    """Stop a detached code worker.

    `cancel_agent_run` refuses any run with a `parent_step_id` — a delegated
    worker runs *inside* its parent's task, so stopping it alone would leave
    the parent waiting on a result that never comes. A code task is different:
    it runs in its own spawned task (`code-task:<id>`), while the lead waits
    in `wait_tasks`, which returns on the cancel. Stopping it alone is safe,
    so this mirrors `cancel_agent_run`'s two in-process branches without the
    delegation guard. Leases release through `_close_log`, as everywhere else.
    """
    import asyncio as _asyncio

    from asgiref.sync import sync_to_async

    from agents.agent.runtime import CANCEL_WAIT_SECONDS

    execution_id = str(log.execution_id)
    if log.status not in ('running', 'pending', 'paused'):
        return log.status
    try:
        live = [t for t in _asyncio.all_tasks()
                if t.get_name() == f'code-task:{execution_id}' and not t.done()]
    except RuntimeError:
        live = []
    if live:
        live[0].cancel()
        try:
            await _asyncio.wait_for(_asyncio.shield(live[0]), CANCEL_WAIT_SECONDS)
        except (_asyncio.CancelledError, _asyncio.TimeoutError,  # noqa: BLE001
                TimeoutError, Exception):
            # Cancelled (the path working as intended), timed out waiting for
            # the unwind, or failed while unwinding — the log below says how
            # the run actually ended either way.
            pass
        await sync_to_async(log.refresh_from_db)()
        return log.status
    if log.status == 'paused':
        from django.utils import timezone

        from agents.agent.runtime import _cancel_pending_hitl, _close_log
        from chat.turn.agent import forget_thread

        await _cancel_pending_hitl(log)
        thread_id = (log.input_data or {}).get('thread_id')
        if thread_id:
            await forget_thread(thread_id)
        await _close_log(log, status='cancelled', result={}, tokens=0,
                         error='Run cancelled while waiting for approval.')
        return 'cancelled'
    raise RuntimeError(
        'This worker is not running in this server process. If it has stopped '
        'responding it will be closed automatically.')


@tool({
    'type': 'function',
    'function': {
        'name': 'revert_task',
        'description': (
            'Revert a finished or stopped worker\'s file changes, newest '
            'first. Refuses any file someone else changed since — that file '
            'is listed, not reverted. Pauses for a human first.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'handle': {'type': 'string'},
            },
            'required': ['handle'],
            'additionalProperties': False,
        },
    },
}, sensitive=True, effect='reversible')
async def revert_task(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async

    from agents.agent import tasks as _tasks
    from workspaces.models import CodeChange, CodeProject

    parent_thread = str(context.get('session_id') or 'run')
    record = _tasks.get(parent_thread, str(args.get('handle') or ''))
    if record is None:
        return json.dumps({'error': f'Unknown task handle {args.get("handle")!r}.'})
    if not record.done.is_set():
        return json.dumps({'error': f'{record.handle} is still {record.status}; stop it first.'})

    project = await CodeProject.objects.filter(
        user_id=context.get('user_id'), id=record.project_id).afirst()
    if project is None:
        return json.dumps({'error': 'The task\'s project is gone.'})
    changes = await sync_to_async(list)(
        CodeChange.objects.filter(run__execution_id=record.execution_id).order_by('-id'))

    from workspaces import engine as _engine

    reverted: list[str] = []
    refused: list[str] = []
    for change in changes:
        path = (change.path or '').strip()
        if not path or path == '(patch)':
            continue
        outcome = await _revert_one(_engine, context, project, change, path)
        (reverted if outcome is None else refused).append(
            path if outcome is None else f'{path}: {outcome}')
    return json.dumps({'reverted': reverted, 'refused': refused})


async def _revert_one(engine, context, project, change, path: str) -> str | None:
    """Revert one file. Returns None on success, the reason on refusal."""
    import hashlib

    from workspaces.engine import WorkspaceError

    full = _join(project, path)
    try:
        ws = await _workspace_of(context)
        try:
            current = bytes(await engine.read(ws, full))
        except WorkspaceError:
            current = None
    except WorkspaceError as exc:
        return str(exc)
    if current is not None and change.after_hash:
        if hashlib.sha256(current).hexdigest() != change.after_hash:
            return ('changed since by someone else; re-read it before deciding '
                    'what it should be')
    try:
        if not change.before_hash:
            # The worker created this file: remove what it made.
            await engine.exec(ws, f'rm {path!r}', cwd=project.workspace_path, timeout=60)
        else:
            # A tracked edit: restore what git had. New-but-committed files
            # land here too, which is also correct — HEAD is what was there.
            out = await engine.exec(
                ws, f'git checkout -- {path!r}', cwd=project.workspace_path, timeout=60)
            if int(out.get('exit_code') or 0) != 0:
                return 'git checkout refused it (untracked or ignored?)'
    except WorkspaceError as exc:
        return str(exc)
    return None


def _join(project, path: str) -> str:
    base = (project.workspace_path or '').rstrip('/')
    rel = str(path or '').strip().lstrip('/')
    return f'{base}/{rel}' if rel else base


async def _workspace_of(context: Dict):
    from django.contrib.auth import get_user_model

    from asgiref.sync import sync_to_async
    from workspaces import engine as _engine

    user = await get_user_model().objects.filter(
        id=context.get('user_id')).afirst()
    if user is None:
        raise _engine.WorkspaceError('No user context.')
    return await sync_to_async(_engine.ensure)(user)
