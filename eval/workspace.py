"""
Per-case workspaces: the files a case starts from, and the files it leaves.

A realistic task is not "answer this question", it is "here is a folder of
messy files, produce these outputs". So a case may carry a workspace spec in
`EvalCase.input_data` under `WORKSPACE_KEY` — kept there, rather than in a new
column, because it is exactly the "extra context the agent is run against"
that field already describes, and a column would be a migration for one key:

    {"__workspace__": {
        "root":  "work/month-end-close",          # relative to the agent's home
        "files": {"orders.csv": "...", "README.md": "..."},
        "watch": ["shared"],                        # extra home-relative paths to snapshot
    }}

**Fresh every attempt.** `prepare` hard-deletes whatever is under `root` (and
under each `watch` path) and writes the fixtures again, so a case can never
pass on a file an earlier attempt left behind — which is exactly how the
first benchmark's file suite once read a sibling case's output.

**Graded on what is there afterwards.** `snapshot` reads every document under
`root` and the `watch` paths into a flat `{path: text}` map that becomes
`GradeContext.files`. Graders stay pure: they never touch the database, they
read the snapshot — the same rule as the rest of `graders.py`. Rendered
binaries (a deck, a workbook, a Word file) are also read as bytes by
`snapshot_binaries`, into `GradeContext.binaries`, because their text extract
cannot say whether a chart exists or a cell is a formula.

**Where the agent sees it.** A `scoped` agent's filesystem is rooted at its home,
so the workspace is `/work/...`; every other mode sees the whole tree, so it is
`/Agents/<home>/work/...`. The goal text says `{workspace}` and is rendered to
whichever the agent will actually be able to use.

Fixtures are written through `inference.vfs` with a `full` scope, i.e. the same
code path an agent's own writes take, so a fixture and an agent-written file are
indistinguishable rows.
"""
from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

WORKSPACE_KEY = '__workspace__'
#: How much of one file a snapshot keeps. A grader asks about specific values,
#: never about megabytes.
SNAPSHOT_FILE_CHARS = 200_000


def spec_for(case) -> dict[str, Any] | None:
    spec = (case.input_data or {}).get(WORKSPACE_KEY)
    return spec if isinstance(spec, dict) and spec.get('root') else None


def _home(user, agent):
    from inference import vfs

    return vfs.agent_home(user, agent.name or '')


def _abs(home_name: str, rel: str) -> str:
    from workflow_backend.thresholds import AGENT_HOME_ROOT

    rel = rel.strip('/')
    return f'/{AGENT_HOME_ROOT}/{home_name}' + (f'/{rel}' if rel else '')


def visible_root(agent, home_name: str, root: str) -> str:
    """The workspace path as this agent's file tools will accept it."""
    mode = (agent.sandbox or {}).get('fileAccess', 'scoped')
    if mode == 'scoped':
        return '/' + root.strip('/')
    return _abs(home_name, root)


def _folder_at(user, abs_path: str):
    """The folder at an absolute path, or None if any segment is missing."""
    from inference import filesystem as fs
    from inference import vfs

    node = None
    for seg in vfs.segments(abs_path):
        node = fs.child_by_name(user, node, seg)
        if node is None:
            return None
    return node


def _wipe(user, abs_path: str) -> None:
    """Hard-delete a folder and everything in it. Benchmark scratch, never the bin."""
    from inference import filesystem as fs
    from inference.models import Document, Folder

    folder = _folder_at(user, abs_path)
    if folder is None:
        return
    folders = list(fs.subtree(folder).values_list('id', flat=True))
    Document._base_manager.filter(user=user, folder_id__in=folders).delete()
    # Deepest first, so no row is deleted before its children.
    for row in Folder.all_objects.filter(id__in=folders).order_by('-path'):
        row.delete()


def _prepare_sync(user, agent, spec: dict) -> str:
    from inference import vfs

    home = _home(user, agent)
    full = vfs.build_scope(user, 'full')
    root = spec['root'].strip('/')
    for rel in [root, *spec.get('watch', [])]:
        _wipe(user, _abs(home.name, rel))
    for rel_path, content in (spec.get('files') or {}).items():
        vfs.write_file(full, _abs(home.name, f'{root}/{rel_path}'), str(content))
    if not spec.get('files'):
        vfs.make_dir(full, _abs(home.name, root))
    return visible_root(agent, home.name, root)


def _documents(user, agent, spec: dict):
    """`(key, document)` for every file under the workspace and the watch paths.

    Keys under `root` are relative to it (`summary.csv`, `replies/T-1.md`); keys
    under a watch path are home-relative and prefixed `~/` (`~/shared/x.csv`),
    so a grader can say "nothing was written here" about a place outside the
    workspace without the two namespaces colliding.
    """
    from inference import filesystem as fs
    from inference.models import Document

    home = _home(user, agent)
    root = spec['root'].strip('/')
    for rel, prefix in [(root, ''), *((w.strip('/'), f'~/{w.strip("/")}/') for w in spec.get('watch', []))]:
        base = _folder_at(user, _abs(home.name, rel))
        if base is None:
            continue
        base_path = fs.name_path(base)
        for doc in (Document.objects.filter(user=user, folder__in=fs.subtree(base, include_trashed=False))
                    .select_related('folder')):
            folder_path = fs.name_path(doc.folder)
            sub = folder_path[len(base_path):].strip('/')
            yield prefix + (f'{sub}/{doc.name}' if sub else doc.name), doc


def _snapshot_sync(user, agent, spec: dict) -> dict[str, str]:
    """`{path: text}` for every file under the workspace and the watch paths."""
    return {key: (doc.content_text or '')[:SNAPSHOT_FILE_CHARS]
            for key, doc in _documents(user, agent, spec)}


def _snapshot_binaries_sync(user, agent, spec: dict) -> dict[str, bytes]:
    """`{path: bytes}` for every rendered binary, keyed like `_snapshot_sync`."""
    from inference.vfs import is_binary
    from workflow_backend.thresholds import AGENT_FILE_BINARY_BYTES

    out: dict[str, bytes] = {}
    for key, doc in _documents(user, agent, spec):
        if not is_binary(doc):
            continue
        with doc.file.open('rb') as handle:
            out[key] = handle.read(AGENT_FILE_BINARY_BYTES)
    return out


async def prepare(user, agent, spec: dict) -> str:
    """Reset the workspace to its fixtures. Returns the path the agent should use."""
    return await sync_to_async(_prepare_sync)(user, agent, spec)


async def snapshot(user, agent, spec: dict) -> dict[str, str]:
    return await sync_to_async(_snapshot_sync)(user, agent, spec)


async def snapshot_binaries(user, agent, spec: dict) -> dict[str, bytes]:
    return await sync_to_async(_snapshot_binaries_sync)(user, agent, spec)


__all__ = ['WORKSPACE_KEY', 'prepare', 'snapshot', 'snapshot_binaries', 'spec_for',
           'visible_root']
