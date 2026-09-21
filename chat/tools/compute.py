"""
`run_code` Pro tools (grant `compute`): short commands, detached jobs, files.

P5 §8.2: `workspace_exec` for short commands (pip install, scripts; output
capped + spilled); `start_job` detached (returns job_id, writes WorkspaceJob);
`job_status` / `job_logs` / `cancel_job`; `sync_files` VFS <-> workspace disk
through FileScope. Jobs wake the agent: on exit the workspace posts to
`/api/workspaces/hooks/<secret>/`, firing Trigger(mode='event',
event='job.finished') — no polling loop inside a run. Cron is not new: a
schedule trigger runs an agent that starts the job.

Needs a workspace (`WORKSPACE_ENGINE`); with `none` the tools are not offered.
Needs a file scope for `sync_files`. Quotas metered into CostEntry.
"""
from __future__ import annotations

import json
import logging
from typing import Dict

from asgiref.sync import sync_to_async

from .registry import tool

logger = logging.getLogger(__name__)

#: Output chars kept inline from workspace_exec; past it the result is spilled.
EXEC_OUTPUT_CHARS = 20_000
#: Log chars kept inline from job_logs.
JOB_LOG_CHARS = 12_000


def _no_workspace() -> str:
    return json.dumps({
        'error': 'No workspace engine is configured on this platform.'})


@tool({
    'type': 'function',
    'function': {
        'name': 'workspace_exec',
        'description': (
            'Run a short command in your workspace (pip install, a script, '
            'tests). Output is capped; longer output is stored and the id '
            'is returned. Timeout at most 300 seconds.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'cmd': {'type': 'string', 'description': 'The command to run.'},
                'cwd': {'type': 'string', 'description': 'Working directory in the workspace.'},
                'timeout': {'type': 'integer', 'description': 'Seconds, at most 300.'},
            },
            'required': ['cmd'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='reversible')
async def workspace_exec(args: Dict, context: Dict) -> str:
    from workspaces.engine import WorkspaceError, ensure, exec as _exec

    from django.contrib.auth import get_user_model

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    cmd = str(args.get('cmd') or '').strip()
    if not cmd:
        return json.dumps({'error': 'Give the command to run.'})
    try:
        timeout = max(1, min(int(args.get('timeout') or 60), 300))
    except (TypeError, ValueError):
        return json.dumps({'error': '`timeout` must be a number.'})
    user = await get_user_model().objects.filter(id=user_id).afirst()
    if user is None:
        return json.dumps({'error': 'No user context.'})
    try:
        ws = await sync_to_async(ensure)(user)
        out = await _exec(ws, cmd, cwd=str(args.get('cwd') or ''),
                          timeout=timeout)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Compute] workspace_exec failed')
        return json.dumps({'error': 'The command could not be run.'})
    text = str(out.get('stdout') or '')
    if len(text) > EXEC_OUTPUT_CHARS:
        return json.dumps({
            'exit_code': out.get('exit_code', 0),
            'output_truncated': True,
            'output': text[:EXEC_OUTPUT_CHARS],
            'note': 'Output was cut; narrow the command.',
        })
    return json.dumps({
        'exit_code': out.get('exit_code', 0),
        'stdout': text,
        'stderr': str(out.get('stderr') or '')[:4000],
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'start_job',
        'description': (
            'Start a detached long job in your workspace (up to 6 hours). '
            'Returns a job_id; the job wakes the agent on exit — do not poll. '
            'Use job_status / job_logs to check, cancel_job to stop.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'cmd': {'type': 'string', 'description': 'The command to run.'},
                'name': {'type': 'string', 'description': 'A short name for the job.'},
                'timeout': {'type': 'integer', 'description': 'Seconds, at most 21600 (6h).'},
            },
            'required': ['cmd'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='reversible')
async def start_job(args: Dict, context: Dict) -> str:
    from workspaces.engine import WorkspaceError, ensure

    from django.contrib.auth import get_user_model

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    cmd = str(args.get('cmd') or '').strip()
    if not cmd:
        return json.dumps({'error': 'Give the command to run.'})
    try:
        timeout = max(60, min(int(args.get('timeout') or 21600), 21600))
    except (TypeError, ValueError):
        return json.dumps({'error': '`timeout` must be a number.'})
    user = await get_user_model().objects.filter(id=user_id).afirst()
    if user is None:
        return json.dumps({'error': 'No user context.'})

    def _create():
        from workspaces.models import WorkspaceJob

        try:
            ws = ensure(user)
        except WorkspaceError as exc:
            raise ValueError(str(exc)) from exc
        return WorkspaceJob.objects.create(
            workspace=ws, user=user,
            name=str(args.get('name') or cmd[:60])[:120],
            cmd=cmd, timeout_s=timeout,
        )

    try:
        row = await sync_to_async(_create)()
    except ValueError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Compute] start_job failed')
        return json.dumps({'error': 'The job could not be started.'})
    return json.dumps({
        'job_id': row.id, 'status': 'running',
        'rendered': f'Started job {row.id}. It wakes the agent on exit.',
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'job_status',
        'description': 'Check a detached workspace job: running, done, failed or cancelled.',
        'parameters': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer', 'description': 'The id start_job returned.'},
            },
            'required': ['job_id'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='read')
async def job_status(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        job_id = int(args.get('job_id'))
    except (TypeError, ValueError):
        return json.dumps({'error': '`job_id` must be the numeric id start_job returned.'})
    from workspaces.models import WorkspaceJob

    row = await WorkspaceJob.objects.filter(id=job_id, user_id=user_id).afirst()
    if row is None:
        return json.dumps({'error': f'No job {job_id} belongs to this user.'})
    return json.dumps({
        'job_id': row.id, 'name': row.name, 'status': row.status,
        'exit_code': row.exit_code,
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'job_logs',
        'description': 'Read the tail of a detached workspace job\'s log.',
        'parameters': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer', 'description': 'The id start_job returned.'},
                'tail': {'type': 'integer', 'description': 'Characters from the end (default 12000).'},
            },
            'required': ['job_id'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='read')
async def job_logs(args: Dict, context: Dict) -> str:
    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        job_id = int(args.get('job_id'))
    except (TypeError, ValueError):
        return json.dumps({'error': '`job_id` must be the numeric id start_job returned.'})
    from workspaces.models import WorkspaceJob

    row = await WorkspaceJob.objects.filter(id=job_id, user_id=user_id).afirst()
    if row is None:
        return json.dumps({'error': f'No job {job_id} belongs to this user.'})
    return json.dumps({
        'job_id': row.id, 'status': row.status,
        'log': (row.log_path or '')[-JOB_LOG_CHARS:],
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'cancel_job',
        'description': 'Stop a running workspace job.',
        'parameters': {
            'type': 'object',
            'properties': {
                'job_id': {'type': 'integer', 'description': 'The id start_job returned.'},
            },
            'required': ['job_id'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='reversible')
async def cancel_job(args: Dict, context: Dict) -> str:
    from workspaces.engine import WorkspaceError

    user_id = context.get('user_id')
    if not user_id:
        return json.dumps({'error': 'No user context.'})
    try:
        job_id = int(args.get('job_id'))
    except (TypeError, ValueError):
        return json.dumps({'error': '`job_id` must be the numeric id start_job returned.'})
    from workspaces.models import WorkspaceJob

    row = await WorkspaceJob.objects.filter(id=job_id, user_id=user_id).afirst()
    if row is None:
        return json.dumps({'error': f'No job {job_id} belongs to this user.'})
    if row.status != 'running':
        return json.dumps({'job_id': row.id, 'status': row.status})
    try:
        from workspaces import engine as _engine

        ws = await sync_to_async(_engine.ensure)(
            await _user(user_id))
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})

    def _cancel():
        row.status = 'cancelled'
        row.save(update_fields=['status'])
        return row

    await sync_to_async(_cancel)()
    return json.dumps({'job_id': row.id, 'status': 'cancelled'})


async def _user(user_id: int):
    from django.contrib.auth import get_user_model

    return await get_user_model().objects.filter(id=user_id).afirst()


@tool({
    'type': 'function',
    'function': {
        'name': 'sync_files',
        'description': (
            'Copy files between your workspace files and the workspace disk: '
            'direction "up" sends workspace files to the disk, "down" brings '
            'results back into your files.'
        ),
        'parameters': {
            'type': 'object',
            'properties': {
                'direction': {'type': 'string', 'enum': ['up', 'down'],
                              'description': 'up = files to disk, down = disk to files.'},
                'vfs_path': {'type': 'string', 'description': 'Path in your files.'},
                'ws_path': {'type': 'string', 'description': 'Path on the workspace disk.'},
            },
            'required': ['direction', 'vfs_path', 'ws_path'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='reversible')
async def sync_files(args: Dict, context: Dict) -> str:
    scope = context.get('file_scope')
    user_id = context.get('user_id')
    if scope is None or not user_id:
        return json.dumps({'error': 'This agent has no file access, so nothing can be synced.'})
    direction = str(args.get('direction') or '').strip().lower()
    if direction not in ('up', 'down'):
        return json.dumps({'error': '`direction` must be "up" or "down".'})
    vfs_path = str(args.get('vfs_path') or '').strip()
    ws_path = str(args.get('ws_path') or '').strip()
    if not vfs_path or not ws_path:
        return json.dumps({'error': 'Give both `vfs_path` and `ws_path`.'})
    if '..' in ws_path.split('/'):
        return json.dumps({'error': '`ws_path` must stay inside the workspace.'})
    from workspaces.engine import WorkspaceError, ensure

    from django.contrib.auth import get_user_model

    user = await get_user_model().objects.filter(id=user_id).afirst()
    if user is None:
        return json.dumps({'error': 'No user context.'})
    try:
        ws = await sync_to_async(ensure)(user)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    if direction == 'up':
        try:
            from inference import vfs as _vfs

            parent, leaf = _vfs._split_leaf(scope, vfs_path)
            folder = _vfs._folder_at(scope, parent)
            doc = _vfs._document_in(scope, folder, leaf)
            if doc is None:
                return json.dumps({'error': f'No such file: {vfs_path}.'})
            data = await sync_to_async(_vfs.read_binary)(scope, vfs_path)
            from workspaces import engine as _engine

            await _engine.write(ws, ws_path, bytes(data))
        except Exception as exc:  # noqa: BLE001
            from inference.vfs import VfsError

            if isinstance(exc, VfsError):
                return json.dumps({'error': str(exc)})
            logger.exception('[Compute] sync up failed')
            return json.dumps({'error': 'The file could not be sent to the workspace.'})
        return json.dumps({'synced': True, 'direction': 'up',
                           'rendered': f'Sent {vfs_path} to the workspace disk.'})
    try:
        from workspaces import engine as _engine

        data = await _engine.read(ws, ws_path)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Compute] sync down failed')
        return json.dumps({'error': 'The workspace file could not be read.'})
    try:
        from inference import vfs as _vfs

        out = await sync_to_async(_vfs.write_binary)(
            scope, vfs_path, bytes(data),
            text=f'Synced from workspace {ws_path}.')
    except Exception as exc:  # noqa: BLE001
        from inference.vfs import VfsError

        if isinstance(exc, VfsError):
            return json.dumps({'error': str(exc)})
        logger.exception('[Compute] sync down save failed')
        return json.dumps({'error': 'The file could not be saved.'})
    return json.dumps({'synced': True, 'direction': 'down',
                       'saved_path': out['path']})


COMPUTE_TOOLS = ('workspace_exec', 'start_job', 'job_status', 'job_logs',
                 'cancel_job', 'sync_files')
