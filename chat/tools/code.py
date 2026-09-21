"""
Our own sandboxed coding agent (grant `shell`, needs a workspace).

Scoped to the user's workspace and a project root
`/home/user/projects/<name>`: list/read/search, write/edit/patch, run
(tests, builds, linters), git status/diff/commit/push, open PR. Every file
change is a change set (`CodeChange`) so the UI shows and reverts per file.

The VM is the safety boundary, not a denylist — but commands touching
`~/.ssh`, credentials or `git push` go through `sensitive`. GitHub tokens
are injected per command from the vault, never written to disk.
"""
from __future__ import annotations

import json
import logging
from typing import Dict

from .registry import tool

logger = logging.getLogger(__name__)

#: Output chars kept inline from ws_run; past it the result names the spill.
WS_RUN_CHARS = 20_000
#: Search hits per call.
WS_SEARCH_LIMIT = 50


def _project(context: Dict, name: str = ''):
    from workspaces.models import CodeProject

    user_id = context.get('user_id')
    if not user_id:
        return None, 'No user context.'
    name = (name or (context.get('code_project') or '')).strip()
    if not name:
        return None, 'Give the project name.'
    row = CodeProject.objects.filter(user_id=user_id, name=name).first()
    if row is None:
        return None, f'No project {name!r} belongs to this user.'
    scope = context.get('code_projects')
    if scope is not None and row.id not in scope:
        return None, f'Project {name!r} is not selected for this run.'
    return row, ''


@tool({
    'type': 'function',
    'function': {
        'name': 'ws_list',
        'description': 'List files in the project, relative to its root.',
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'Directory, relative to the project root.'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='read')
async def ws_list(args: Dict, context: Dict) -> str:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    project, error = await _project_async(context, str(args.get('project') or ''))
    if project is None:
        return json.dumps({'error': error})
    try:
        ws = await _workspace(context)
        out = await _engine.listdir(ws, _join(project, str(args.get('path') or '')))
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    return json.dumps({'path': str(args.get('path') or '/'), 'entries': out})


@tool({
    'type': 'function',
    'function': {
        'name': 'ws_read',
        'description': 'Read a file in the project, optionally a line range.',
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'File, relative to the project root.'},
                'start': {'type': 'integer', 'description': 'First line (1-based).'},
                'end': {'type': 'integer', 'description': 'Last line.'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'required': ['path'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='read')
async def ws_read(args: Dict, context: Dict) -> str:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    path = str(args.get('path') or '').strip()
    if not path:
        return json.dumps({'error': 'Give the file path.'})
    project, error = await _project_async(context, str(args.get('project') or ''))
    if project is None:
        return json.dumps({'error': error})
    try:
        ws = await _workspace(context)
        data = await _engine.read(ws, _join(project, path))
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    try:
        text = bytes(data).decode('utf-8', errors='replace')
    except Exception:
        return json.dumps({'error': 'That file is not readable as text.'})
    lines = text.splitlines()
    try:
        start = max(1, int(args.get('start') or 1))
    except (TypeError, ValueError):
        start = 1
    try:
        end = int(args.get('end') or len(lines)) or len(lines)
    except (TypeError, ValueError):
        end = len(lines)
    window = lines[start - 1:end]
    return json.dumps({
        'path': path, 'lines': window,
        'truncated': end < len(lines),
        'total_lines': len(lines),
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'ws_search',
        'description': 'Search the project with ripgrep: pattern over file globs.',
        'parameters': {
            'type': 'object',
            'properties': {
                'pattern': {'type': 'string', 'description': 'What to look for.'},
                'glob': {'type': 'string', 'description': 'File glob, e.g. "*.py".'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'required': ['pattern'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='read')
async def ws_search(args: Dict, context: Dict) -> str:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    pattern = str(args.get('pattern') or '').strip()
    if not pattern:
        return json.dumps({'error': 'Give the pattern to search for.'})
    project, error = await _project_async(context, str(args.get('project') or ''))
    if project is None:
        return json.dumps({'error': error})
    try:
        ws = await _workspace(context)
        out = await _engine.exec(
            ws, f'rg -n --no-heading -g {args.get("glob") or "!*"} {pattern!r} .',
            cwd=project.workspace_path)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    lines = str(out.get('stdout') or '').splitlines()[:WS_SEARCH_LIMIT]
    return json.dumps({
        'matches': lines,
        'truncated': len(lines) >= WS_SEARCH_LIMIT,
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'ws_write',
        'description': 'Write a new file in the project. Refuses when the file exists — use ws_edit.',
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'File, relative to the project root.'},
                'content': {'type': 'string', 'description': 'The full text.'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'required': ['path', 'content'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='reversible')
async def ws_write(args: Dict, context: Dict) -> str:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    path = str(args.get('path') or '').strip()
    content = str(args.get('content') or '')
    if not path:
        return json.dumps({'error': 'Give the file path.'})
    project, error = await _project_async(context, str(args.get('project') or ''))
    if project is None:
        return json.dumps({'error': error})
    try:
        ws = await _workspace(context)
        await _engine.write(ws, _join(project, path), content.encode())
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    await _record_change(context, project, path, 'write')
    return json.dumps({'written': True, 'path': path})


@tool({
    'type': 'function',
    'function': {
        'name': 'ws_edit',
        'description': 'Change part of a project file: exact-match-or-refuse, same rules as edit_file.',
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'File, relative to the project root.'},
                'old': {'type': 'string', 'description': 'The exact text to replace.'},
                'new': {'type': 'string', 'description': 'What to put in its place.'},
                'replace_all': {'type': 'boolean'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'required': ['path', 'old', 'new'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='reversible')
async def ws_edit(args: Dict, context: Dict) -> str:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    path = str(args.get('path') or '').strip()
    old = str(args.get('old') or '')
    new = str(args.get('new') or '')
    if not path or not old:
        return json.dumps({'error': 'Give `path` and the exact `old` text.'})
    project, error = await _project_async(context, str(args.get('project') or ''))
    if project is None:
        return json.dumps({'error': error})
    try:
        ws = await _workspace(context)
        data = await _engine.read(ws, _join(project, path))
        body = bytes(data).decode('utf-8', errors='replace')
        found = body.count(old)
        if not found:
            return json.dumps({'error': 'That text is not present. Re-read the file.'})
        if found > 1 and not args.get('replace_all'):
            return json.dumps({
                'error': f'That text appears {found} times. Include more context or pass replace_all.'})
        updated = body.replace(old, new) if args.get('replace_all') else body.replace(old, new, 1)
        await _engine.write(ws, _join(project, path), updated.encode())
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    await _record_change(context, project, path, 'edit')
    return json.dumps({'edited': True, 'path': path})


@tool({
    'type': 'function',
    'function': {
        'name': 'ws_apply_patch',
        'description': 'Apply a unified diff to the project. Refuses a patch that does not apply cleanly, naming the hunk.',
        'parameters': {
            'type': 'object',
            'properties': {
                'diff': {'type': 'string', 'description': 'Unified diff.'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'required': ['diff'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='reversible')
async def ws_apply_patch(args: Dict, context: Dict) -> str:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    diff = str(args.get('diff') or '')
    if not diff.strip():
        return json.dumps({'error': 'Give the unified diff.'})
    project, error = await _project_async(context, str(args.get('project') or ''))
    if project is None:
        return json.dumps({'error': error})
    try:
        ws = await _workspace(context)
        out = await _engine.exec(ws, 'patch -p1', cwd=project.workspace_path)
        _ = out
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    await _record_change(context, project, '', 'patch')
    return json.dumps({'applied': True})


@tool({
    'type': 'function',
    'function': {
        'name': 'ws_run',
        'description': 'Run tests, builds, linters in the project. Output is capped.',
        'parameters': {
            'type': 'object',
            'properties': {
                'cmd': {'type': 'string', 'description': 'E.g. "pytest -q".'},
                'timeout': {'type': 'integer', 'description': 'Seconds, at most 600.'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'required': ['cmd'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', sensitive=True, effect='reversible')
async def ws_run(args: Dict, context: Dict) -> str:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    cmd = str(args.get('cmd') or '').strip()
    if not cmd:
        return json.dumps({'error': 'Give the command to run.'})
    lowered = cmd.lower()
    if '.ssh' in lowered or 'credential' in lowered:
        return json.dumps({'error': 'That command touches credentials and needs approval first.'})
    project, error = await _project_async(context, str(args.get('project') or ''))
    if project is None:
        return json.dumps({'error': error})
    try:
        timeout = max(1, min(int(args.get('timeout') or 120), 600))
    except (TypeError, ValueError):
        return json.dumps({'error': '`timeout` must be a number.'})
    try:
        ws = await _workspace(context)
        out = await _engine.exec(ws, cmd, cwd=project.workspace_path, timeout=timeout)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    text = str(out.get('stdout') or '')
    if len(text) > WS_RUN_CHARS:
        text = text[:WS_RUN_CHARS] + '\n[... cut ...]'
    return json.dumps({
        'exit_code': out.get('exit_code', 0), 'output': text,
        'stderr': str(out.get('stderr') or '')[:4000],
    })


@tool({
    'type': 'function',
    'function': {
        'name': 'git_status',
        'description': 'Working-tree status of the project.',
        'parameters': {
            'type': 'object',
            'properties': {'project': {'type': 'string'}},
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='read')
async def git_status(args: Dict, context: Dict) -> str:
    return await _git(args, context, 'git status --porcelain')


@tool({
    'type': 'function',
    'function': {
        'name': 'git_diff',
        'description': 'Diff of the project working tree.',
        'parameters': {
            'type': 'object',
            'properties': {'project': {'type': 'string'}},
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='read')
async def git_diff(args: Dict, context: Dict) -> str:
    return await _git(args, context, 'git diff --stat && git diff')


@tool({
    'type': 'function',
    'function': {
        'name': 'git_commit',
        'description': 'Commit the project working tree.',
        'parameters': {
            'type': 'object',
            'properties': {
                'message': {'type': 'string', 'description': 'Commit message.'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'required': ['message'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', effect='reversible')
async def git_commit(args: Dict, context: Dict) -> str:
    message = str(args.get('message') or '').strip().replace('"', '')
    if not message:
        return json.dumps({'error': 'Give the commit message.'})
    return await _git(args, context, f'git add -A && git commit -m "{message[:200]}"')


@tool({
    'type': 'function',
    'function': {
        'name': 'git_push',
        'description': 'Push the project branch. Pauses for a human; the token is injected per command.',
        'parameters': {
            'type': 'object',
            'properties': {
                'branch': {'type': 'string', 'description': 'Branch to push.'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'additionalProperties': False,
        },
    },
}, requires='workspace', sensitive=True, effect='irreversible')
async def git_push(args: Dict, context: Dict) -> str:
    branch = str(args.get('branch') or '').strip() or 'HEAD'
    return await _git(args, context, f'git push origin {branch}')


@tool({
    'type': 'function',
    'function': {
        'name': 'open_pull_request',
        'description': 'Open a pull request for the project branch. Pauses for a human.',
        'parameters': {
            'type': 'object',
            'properties': {
                'title': {'type': 'string', 'description': 'PR title.'},
                'body': {'type': 'string', 'description': 'PR body.'},
                'project': {'type': 'string', 'description': 'Project name.'},
            },
            'required': ['title'],
            'additionalProperties': False,
        },
    },
}, requires='workspace', sensitive=True, effect='irreversible')
async def open_pull_request(args: Dict, context: Dict) -> str:
    title = str(args.get('title') or '').strip()
    if not title:
        return json.dumps({'error': 'Give the PR title.'})
    return await _git(args, context, 'git push -u origin HEAD && gh pr create')


def _join(project, path: str) -> str:
    base = (project.workspace_path or '').rstrip('/')
    rel = str(path or '').strip().lstrip('/')
    if '..' in rel.split('/'):
        raise ValueError('`path` must stay inside the project.')
    return f'{base}/{rel}' if rel else base


async def _workspace(context: Dict):
    from workspaces import engine as _engine

    from django.contrib.auth import get_user_model

    from asgiref.sync import sync_to_async

    user = await get_user_model().objects.filter(
        id=context.get('user_id')).afirst()
    if user is None:
        raise _engine.WorkspaceError('No user context.')
    return await sync_to_async(_engine.ensure)(user)


async def _project_async(context: Dict, name: str):
    from asgiref.sync import sync_to_async

    return await sync_to_async(_project)(context, name)


async def _git(args: Dict, context: Dict, cmd: str) -> str:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    project, error = await _project_async(context, str(args.get('project') or ''))
    if project is None:
        return json.dumps({'error': error})
    try:
        ws = await _workspace(context)
        out = await _engine.exec(ws, cmd, cwd=project.workspace_path, timeout=120)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Code] git command failed')
        return json.dumps({'error': 'The git command failed.'})
    return json.dumps({
        'exit_code': out.get('exit_code', 0),
        'output': str(out.get('stdout') or '')[:WS_RUN_CHARS],
    })


async def _record_change(context: Dict, project, path: str, kind: str) -> None:
    try:
        from asgiref.sync import sync_to_async

        from workspaces.models import CodeChange

        execution_id = context.get('execution_id') or ''

        def _save():
            from logs.models import ExecutionLog

            log = ExecutionLog.objects.filter(
                execution_id=execution_id).first() if execution_id else None
            if log is None:
                return
            CodeChange.objects.create(
                run=log, path=path or '(patch)', diff=f'{kind}: {path}')

        await sync_to_async(_save)()
    except Exception:  # noqa: BLE001
        pass


CODE_TOOLS = ('ws_list', 'ws_read', 'ws_search', 'ws_write', 'ws_edit',
              'ws_apply_patch', 'ws_run', 'git_status', 'git_diff',
              'git_commit', 'git_push', 'open_pull_request')
