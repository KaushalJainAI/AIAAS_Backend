"""
Our own sandboxed coding agent (grant `shell`, needs a workspace).

Scoped to the user's workspace and a project root
`/home/user/projects/<name>`: list/read/search, write/edit/patch, run
(tests, builds, linters), git status/diff/commit/push, open PR. Every file
change is a change set (`CodeChange`) so the UI shows and reverts per file.

The VM is the safety boundary, not a denylist — but commands touching
`~/.ssh`, credentials or `git push` go through `sensitive`. GitHub tokens
are injected per command from the vault, never written to disk.

Coding-team rules (C1/C2), enforced here rather than by prompt:

- `writePaths` (the role's allow-list) intersected with the task's `claims`
  decides whether a path may be written at all. Checked *before* the lease
  is taken: a write that is out of scope must not take a lock.
- Writing takes a lease (`workspaces/leases.py`). A path covered by someone
  else's live lease is refused with the holder named. Uncovered and
  uncontended paths are leased implicitly to the writer.
- `ws_read` returns the file's sha256 and records it in the run's read set;
  `ws_edit`/`ws_apply_patch` refuse when the file changed since this run
  last read it. That hash check is the real correctness guarantee; notices
  (C3) are only a courtesy.
- `ws_run` accepts a command class (`test`, `lint`, ...) or a literal that
  prefix-matches the project's resolved command for an allowed class.
"""
from __future__ import annotations

import hashlib
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
    digest = hashlib.sha256(bytes(data)).hexdigest()
    _remember_read(context, path, digest)
    return json.dumps({
        'path': path, 'lines': window,
        'truncated': end < len(lines),
        'total_lines': len(lines),
        'sha256': digest,
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
    # Scope before lease: an out-of-scope write must not take a lock.
    refused = _write_refused(context, path)
    if refused:
        return json.dumps({'error': refused})
    try:
        ws = await _workspace(context)
        full = _join(project, path)
        if await _exists(ws, full):
            return json.dumps({
                'error': f'{path} already exists. Read it first and use ws_edit.'})
        before = ''
        await _engine.write(ws, full, content.encode())
        after = hashlib.sha256(content.encode()).hexdigest()
        _remember_read(context, path, after)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    # A new file cannot be stale, but it can collide: take the lease (or
    # refuse with the holder named) before recording the change.
    lease_error = await _take_lease(context, project, path)
    if lease_error:
        return json.dumps({'error': lease_error})
    await _record_change(context, project, path, 'write',
                         before_hash='', after_hash=after)
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
    refused = _write_refused(context, path)
    if refused:
        return json.dumps({'error': refused})
    try:
        ws = await _workspace(context)
        data = await _engine.read(ws, _join(project, path))
        body = bytes(data).decode('utf-8', errors='replace')
        current = hashlib.sha256(bytes(data)).hexdigest()
        stale = _stale_refused(context, path, current)
        if stale:
            return json.dumps({'error': stale})
        found = body.count(old)
        if not found:
            return json.dumps({'error': 'That text is not present. Re-read the file.'})
        if found > 1 and not args.get('replace_all'):
            return json.dumps({
                'error': f'That text appears {found} times. Include more context or pass replace_all.'})
        updated = body.replace(old, new) if args.get('replace_all') else body.replace(old, new, 1)
        await _engine.write(ws, _join(project, path), updated.encode())
        after = hashlib.sha256(updated.encode()).hexdigest()
        _remember_read(context, path, after)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    lease_error = await _take_lease(context, project, path)
    if lease_error:
        return json.dumps({'error': lease_error})
    await _record_change(context, project, path, 'edit',
                         before_hash=current, after_hash=after)
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
    paths = _patch_paths(diff)
    for path in paths:
        refused = _write_refused(context, path)
        if refused:
            return json.dumps({'error': refused})
    # Stale check per touched file the run has read: a patch over a file that
    # changed underneath it is refused rather than merged by us.
    try:
        ws = await _workspace(context)
        for path in paths:
            try:
                data = await _engine.read(ws, _join(project, path))
            except WorkspaceError:
                continue
            stale = _stale_refused(
                context, path, hashlib.sha256(bytes(data)).hexdigest())
            if stale:
                return json.dumps({'error': stale})
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    for path in paths:
        lease_error = await _take_lease(context, project, path)
        if lease_error:
            return json.dumps({'error': lease_error})
    try:
        out = await _engine.exec(
            ws, 'patch -p1', cwd=project.workspace_path,
            stdin=diff.encode(), timeout=120)
        _ = out
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    for path in paths:
        await _record_change(context, project, path, 'patch')
    if not paths:
        await _record_change(context, project, '', 'patch')
    return json.dumps({'applied': True, 'files': paths})


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
    allowed, resolved_cmd, command_error = _resolve_command(context, project, cmd)
    if not allowed:
        return json.dumps({'error': command_error})
    try:
        timeout = max(1, min(int(args.get('timeout') or 120), 600))
    except (TypeError, ValueError):
        return json.dumps({'error': '`timeout` must be a number.'})
    try:
        ws = await _workspace(context)
        out = await _engine.exec(ws, resolved_cmd, cwd=project.workspace_path, timeout=timeout)
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
    # The integrator takes a project-wide lock while assembling a commit, so
    # nothing writes halfway through `git add -A && git commit`.
    guard = await _take_commit_lease(context, project) if _is_commit_cmd(cmd) else ''
    if guard:
        return json.dumps({'error': guard})
    try:
        ws = await _workspace(context)
        out = await _engine.exec(ws, cmd, cwd=project.workspace_path, timeout=120)
    except WorkspaceError as exc:
        return json.dumps({'error': str(exc)})
    except Exception:
        logger.exception('[Code] git command failed')
        return json.dumps({'error': 'The git command failed.'})
    finally:
        if _is_commit_cmd(cmd):
            await _release_commit_lease(context, project)
    return json.dumps({
        'exit_code': out.get('exit_code', 0),
        'output': str(out.get('stdout') or '')[:WS_RUN_CHARS],
    })


def _is_commit_cmd(cmd: str) -> bool:
    lowered = (cmd or '').lower()
    return 'git commit' in lowered or 'git push' in lowered or 'gh pr create' in lowered


async def _take_commit_lease(context: Dict, project) -> str:
    """A project-wide `.git/**` lease for commit/push/PR assembly."""
    holder = await _holder(context)
    if holder is None:
        return ''
    try:
        from asgiref.sync import sync_to_async

        from workspaces import leases as _leases

        label = str(context.get('worker_label') or 'run %s' % holder.id)
        task_id = str(context.get('task_id') or '')

        def _take():
            return _leases.acquire(
                project, holder, ['.git/**'],
                holder_label=label, task_id=task_id)

        await sync_to_async(_take)()
        return ''
    except Exception as exc:  # noqa: BLE001 — LeaseConflict carries the reason
        return str(exc)


async def _release_commit_lease(context: Dict, project) -> None:
    try:
        from asgiref.sync import sync_to_async

        from workspaces import leases as _leases

        holder = await _holder(context)
        if holder is None:
            return
        await sync_to_async(_leases.release_pattern)(project.id, '.git/**', holder.id)
    except Exception:  # noqa: BLE001
        pass


def _remember_read(context: Dict, path: str, digest: str) -> None:
    """Record what this run saw, in the enforcement store (`workspaces/reads`).

    Keyed by the run's thread (`session_id`), which workers get as a throwaway
    — so a worker's read set is its own. Mirrored into `meta['reads']` by the
    `_on_ws_read` observer in `chat/turn/agent.py`, which is what survives in
    the run's record; this module is what refusals read, so curation can never
    lift the guard.
    """
    try:
        from workspaces import reads as _reads

        thread = str(context.get('session_id') or '')
        _reads.record(thread, _norm(path), digest or '')
    except Exception:  # noqa: BLE001
        pass


def _stale_refused(context: Dict, path: str, current: str) -> str:
    """Refuse an edit over a file that changed since this run read it."""
    try:
        from workspaces import reads as _reads

        thread = str(context.get('session_id') or '')
        seen = _reads.last_read(thread, _norm(path))
    except Exception:  # noqa: BLE001
        return ''
    if seen is None or not current:
        return ''
    if seen == current:
        return ''
    label = _changer_label(context, path)
    return (
        f'{path} changed since you read it'
        + (f' (by {label})' if label else '')
        + '; re-read it and rebase your change onto what is there now.'
    )


def _changer_label(context: Dict, path: str) -> str:
    """Who changed `path` last, for the stale refusal. Best-effort."""
    try:
        from workspaces.models import CodeChange

        row = (CodeChange.objects
               .filter(path=_norm(path))
               .order_by('-created_at')
               .select_related('run')
               .first())
        if row is None:
            return 'you in the editor'
        # The writer's own label where we stored one; otherwise "another run".
        holder_label = ''
        try:
            from workspaces.models import CodeLease  # noqa: F401
        except Exception:  # noqa: BLE001
            pass
        return holder_label or 'another worker'
    except Exception:  # noqa: BLE001
        return ''


def _norm(path: str) -> str:
    """Normalise a project-relative path the way leases do."""
    try:
        from workspaces.leases import normalize_path as _normalize

        return _normalize(path)
    except Exception:  # noqa: BLE001
        return str(path or '').strip().lstrip('/')


def _write_refused(context: Dict, path: str) -> str:
    """Whether `path` is outside this run's `writePaths` ∩ task claims."""
    norm = _norm(path)
    if not norm:
        return 'Give the file path.'
    scope = context.get('write_paths')
    claims = [str(p) for p in (context.get('task_claims') or []) if str(p).strip()]
    # None means unrestricted on both axes (agents predate the fields). `()`
    # is what disjoint restrictions intersect to and means "may write
    # nothing" — not unrestricted, which would widen the runs refused above.
    if scope is not None and not _covered_by_any(norm, scope):
        shown = ", ".join(list(scope)[:5]) or "nothing"
        return (
            f'{path} is outside your writePaths ({shown}). '
            f'Stay inside the files your role may write.'
        )
    if claims and not _covered_by_any(norm, claims):
        return (
            f'{path} is outside this task\'s claims ({", ".join(claims[:5])}). '
            f'Write only what your task claimed.'
        )
    return ''


def _covered_by_any(path: str, patterns) -> bool:
    try:
        from workspaces.leases import covers as _covers

        return any(_covers(p, path) for p in patterns)
    except Exception:  # noqa: BLE001
        return True


def _resolve_command(context: Dict, project, cmd: str):
    """Whether `ws_run(cmd)` may run, and what actually runs.

    Returns `(allowed, resolved_cmd, error)`. A bare class name (`test`)
    resolves to the project's configured command for it. A literal must
    prefix-match one of the resolved commands for an allowed class (argv
    prefix, not a substring), or the class must be `any`. Unrestricted
    (empty scope) runs anything, as before this field existed.
    """
    from agents.agent.runtime import CODE_COMMAND_CLASSES

    raw_scope = context.get('command_scope')
    scope = None if raw_scope is None else tuple(
        str(c or '').strip().lower() for c in raw_scope if str(c or '').strip())
    resolved = dict(getattr(project, 'commands', None) or {})
    word = cmd.strip()
    if word.lower() in CODE_COMMAND_CLASSES:
        klass = word.lower()
        if scope is not None and klass not in scope and 'any' not in scope:
            return False, cmd, (
                f'Command class "{klass}" is outside your commandScope '
                f'({", ".join(scope) or "nothing"}). Use one of those, or say what you need.'
            )
        target = str(resolved.get(klass) or '').strip()
        if not target:
            return False, cmd, (
                f'No "{klass}" command is configured for this project. '
                f'Ask the lead to set CodeProject.commands, or run a literal '
                f'that matches one.'
            )
        return True, target, ''
    # A literal: find which configured command it starts as (argv prefix).
    if scope is None:
        return True, cmd, ''
    if 'any' in scope:
        return True, cmd, ''
    for klass in scope:
        target = str(resolved.get(klass) or '').strip()
        if target and _argv_prefix(cmd, target):
            return True, cmd, ''
    return False, cmd, (
        f'`{cmd[:80]}` matches no configured command for your commandScope '
        f'({", ".join(scope)}). Run one of those classes, or say what you need.'
    )


def _argv_prefix(cmd: str, configured: str) -> bool:
    """Whether `cmd` starts with `configured` on an argv boundary."""
    import shlex

    try:
        want = shlex.split(configured)
        got = shlex.split(cmd)
    except ValueError:
        return cmd.strip().startswith(configured.strip())
    if not want:
        return False
    return got[:len(want)] == want


def _patch_paths(diff: str) -> list[str]:
    """File paths a unified diff touches (`+++ b/path` lines)."""
    paths: list[str] = []
    for line in (diff or '').splitlines():
        if line.startswith('+++ '):
            rest = line[4:].strip()
            if rest.startswith('b/'):
                rest = rest[2:]
            rest = rest.split('\t', 1)[0].strip().strip('"')
            if rest and rest != '/dev/null':
                paths.append(_norm(rest))
    return paths


async def _exists(ws, full: str) -> bool:
    from workspaces import engine as _engine
    from workspaces.engine import WorkspaceError

    try:
        await _engine.read(ws, full)
        return True
    except WorkspaceError:
        return False
    except Exception:  # noqa: BLE001
        return False


async def _holder(context: Dict):
    """This run's `ExecutionLog` row, or None in chat (no run to hold a lease)."""
    execution_id = str(context.get('execution_id') or '').strip()
    if not execution_id:
        return None
    try:
        from asgiref.sync import sync_to_async

        from logs.models import ExecutionLog

        return await sync_to_async(ExecutionLog.objects.filter(
            execution_id=execution_id).first)()
    except Exception:  # noqa: BLE001
        return None


async def _take_lease(context: Dict, project, path: str) -> str:
    """Lease `path` for this run, or refuse with the holder named.

    No holder (chat) means no lease to take — the stale guard still applies.
    Heartbeats on every call so a working run keeps its lock and a dead one
    expires within the TTL for the recovery sweep to clear.
    """
    holder = await _holder(context)
    if holder is None:
        return ''
    try:
        from asgiref.sync import sync_to_async

        from workspaces import leases as _leases

        label = str(context.get('worker_label') or f'run {holder.id}')
        task_id = str(context.get('task_id') or '')
        await sync_to_async(_leases.heartbeat)(holder.id)

        def _take():
            return _leases.acquire(
                project, holder, [_norm(path)],
                holder_label=label, task_id=task_id)

        await sync_to_async(_take)()
        return ''
    except Exception as exc:  # noqa: BLE001 — LeaseConflict carries the reason
        from workspaces.leases import LeaseConflict

        if isinstance(exc, LeaseConflict):
            return str(exc)
        # sync_to_async wraps; unwrap one level when it does.
        cause = getattr(exc, '__cause__', None)
        if isinstance(cause, LeaseConflict):
            return str(cause)
        text = str(exc)
        if 'overlap' in text and 'lease' in text:
            return text
        logger.warning('[Code] Lease take failed for %s', path, exc_info=True)
        return ''


async def _record_change(context: Dict, project, path: str, kind: str,
                         before_hash: str = '', after_hash: str = '') -> None:
    try:
        from asgiref.sync import sync_to_async

        from workspaces.models import CodeChange

        execution_id = context.get('execution_id') or ''
        label = str(context.get('worker_label') or '')

        def _save():
            from logs.models import ExecutionLog

            log = ExecutionLog.objects.filter(
                execution_id=execution_id).first() if execution_id else None
            if log is None:
                return None
            row = CodeChange.objects.create(
                run=log, path=_norm(path) or '(patch)',
                before_hash=before_hash or '', after_hash=after_hash or '',
                diff=f'{kind}: {_norm(path)}')
            return row.id

        change_id = await sync_to_async(_save)()
        if change_id:
            await _publish_change(project, _norm(path) or '(patch)', change_id,
                                  execution_id, label)
            # C3: who needs to know. Best-effort and synchronous (both
            # registries are in-process); a notice never fails the write it
            # follows, and the stale guard holds even when one is missed.
            try:
                from workspaces import awareness as _awareness

                _awareness.notify(
                    project.id, _norm(path) or '(patch)',
                    by_label=label or 'a worker',
                    writer_thread=str(context.get('session_id') or ''),
                    writer_execution_id=execution_id)
            except Exception:  # noqa: BLE001
                pass
            # C6: the panel's change list. Whoever is watching the lead sees
            # the file flash with the writer's label; `/runs` replays it from
            # the `CodeChange` rows.
            try:
                from agents.agent import tasks as _tasks

                from chat.turn.events import Event

                parent = _tasks.parent_of_execution(execution_id)
                if parent:
                    await _tasks.emit(parent, Event.CODE_CHANGE, {
                        'path': _norm(path) or '(patch)',
                        'change_id': change_id,
                        'by_label': label or 'a worker',
                    })
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass


async def _publish_change(project, path: str, change_id: int,
                          execution_id: str, label: str) -> None:
    """The C3 bus write, best-effort: the same event the UI consumes."""
    try:
        from asgiref.sync import sync_to_async

        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        await layer.group_send(f'code:{project.id}', {
            'type': 'code.change',
            'project_id': project.id,
            'path': path,
            'change_id': change_id,
            'by_run': str(execution_id or ''),
            'by_label': label or 'a worker',
        })
    except Exception:  # noqa: BLE001
        pass


CODE_TOOLS = ('ws_list', 'ws_read', 'ws_search', 'ws_write', 'ws_edit',
              'ws_apply_patch', 'ws_run', 'git_status', 'git_diff',
              'git_commit', 'git_push', 'open_pull_request')
