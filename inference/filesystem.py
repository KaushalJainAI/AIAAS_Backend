"""
The one door between a client-supplied id and a `Folder`.

**No view, serializer, task or tool may turn an inbound folder id into a Folder
any other way.** A file system multiplies the ways one id can reach another
user's data — a foreign `parent_id` on create, a foreign move target, a restore
that reparents, a path string used as a locator — and answering each of those
with its own `.filter(user=...)` means the guarantee is only as good as the
newest developer's memory. Here it is one function, `resolve_folder`, and the
reviewable invariant is that `Folder.objects` appears nowhere else
(`inference/tests/test_filesystem.py::ChokePointTests` enforces it).

Three properties that are load-bearing rather than stylistic:

*Root is None.* `folder_id IS NULL` is the user's root; there is no root row.
NULL is unforgeable — it can never be another user's folder — so the most-used
location in the tree is not an id anyone can guess wrong.

*Unknown and foreign are the same answer.* `resolve_folder` raises
`FolderNotFound` for both, and the views turn that into 404. A 403 for "exists
but not yours" would be an ownership oracle, letting anyone map which ids are
real — the same reasoning that makes `/api/orchestrator/hooks/<secret>/` answer
404 for every refusal.

*`path` holds ids, not names.* Rename becomes one column write instead of
O(descendants), cycle detection is a string comparison with no queries, and a
subtree is one indexed prefix match — which is what makes trash, restore and
purge tractable without a recursive CTE. It also keeps a user-controlled path
string out of the database entirely.

This module never imports `inference.tasks`. That is decision "folders organise,
KBs index" made structural: the move path *cannot* re-index a document, because
it has no way to reach the code that would.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Sequence

from django.db import transaction
from django.db.models import F, Value
from django.db.models.functions import Replace
from django.utils import timezone

from workflow_backend.thresholds import (
    MAX_FOLDER_DEPTH,
    MAX_FOLDERS_PER_USER,
    MAX_MOVE_BATCH,
)

from .models import Document, Folder

logger = logging.getLogger(__name__)

#: Accepted spellings of "the root" on the wire, besides an absent value.
_ROOT_TOKENS = {'', 'root', 'null', 'none'}

#: Characters a folder name may not contain. Not a path-traversal guard — no
#: name ever reaches a filesystem or a URL — but a name containing a separator
#: would render as a fake hierarchy in every client that shows a path.
_ILLEGAL_NAME = re.compile(r'[/\\\x00-\x1f]')

#: Trailing " (2)" that `unique_name` adds and re-parses, so repeated
#: collisions do not stack up as "report (1) (1) (1)".
_SUFFIXED = re.compile(r'^(?P<stem>.*?) \((?P<n>\d+)\)$')


class FolderNotFound(Exception):
    """Unknown id, or an id belonging to someone else. Deliberately one
    exception for both — see the module docstring. Views answer 404."""

    def __init__(self, missing: Sequence = (), kind: str = 'folder'):
        self.missing = list(missing)
        self.kind = kind
        super().__init__(
            f'Unknown {kind}s (not yours or not found): {sorted(self.missing)}'
            if self.missing else f'That {kind} does not exist.'
        )


class FilesystemError(Exception):
    """A request that is well-formed and authorised but cannot be honoured —
    a cycle, a depth or count cap, a duplicate name. Views answer 400."""


# ---------------------------------------------------------------------------
# Resolution — the choke point itself
# ---------------------------------------------------------------------------

def resolve_folder(user, folder_id, *, include_trashed: bool = False) -> Folder | None:
    """Turn a client-supplied folder id into one of `user`'s folders.

    `None`, `''`, `'root'` and `'null'` all resolve to `None`, which *is* the
    user's root — not a sentinel for "unspecified". Raises `FolderNotFound`
    for an id that does not exist and for one that exists but belongs to
    somebody else, without distinguishing them.
    """
    if folder_id is None:
        return None
    if isinstance(folder_id, str) and folder_id.strip().lower() in _ROOT_TOKENS:
        return None
    if isinstance(folder_id, Folder):
        # Already resolved by a caller that went through this function.
        if folder_id.user_id != user.id:
            raise FolderNotFound([folder_id.pk])
        return folder_id

    try:
        pk = int(folder_id)
    except (TypeError, ValueError):
        raise FolderNotFound([folder_id])

    manager = Folder.all_objects if include_trashed else Folder.objects
    folder = manager.filter(pk=pk, user=user).first()
    if folder is None:
        raise FolderNotFound([pk])
    return folder


def resolve_folders(user, ids: Iterable, *, include_trashed: bool = False) -> list[Folder]:
    """Bulk `resolve_folder`. Any id that does not resolve fails the whole
    call, naming the missing set — the `agents/views/agents.py::_owned_ids`
    shape, so a partially-applied bulk operation is impossible."""
    wanted = _clean_id_batch(ids, 'folder')
    if not wanted:
        return []
    manager = Folder.all_objects if include_trashed else Folder.objects
    found = list(manager.filter(pk__in=wanted, user=user))
    missing = wanted - {f.pk for f in found}
    if missing:
        raise FolderNotFound(missing)
    return found


def resolve_documents(user, ids: Iterable, *, include_trashed: bool = False) -> list[Document]:
    """Bulk resolution for documents, same contract as `resolve_folders`."""
    wanted = _clean_id_batch(ids, 'document')
    if not wanted:
        return []
    manager = Document.all_objects if include_trashed else Document.objects
    found = list(manager.filter(pk__in=wanted, user=user))
    missing = wanted - {d.pk for d in found}
    if missing:
        raise FolderNotFound(missing, kind='document')
    return found


def _clean_id_batch(ids: Iterable, kind: str) -> set:
    if ids is None:
        return set()
    if not isinstance(ids, (list, tuple, set, frozenset)):
        raise FilesystemError(f'{kind}_ids must be a list of integers.')
    if len(ids) > MAX_MOVE_BATCH:
        raise FilesystemError(f'At most {MAX_MOVE_BATCH} {kind}s per request.')
    out = set()
    for raw in ids:
        # bool is an int subclass; True would silently become id 1.
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise FolderNotFound([raw], kind=kind)
        out.add(raw)
    return out


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------

def subtree(folder: Folder, *, include_trashed: bool = True):
    """Every folder at or beneath `folder`, itself included.

    One indexed prefix match rather than a recursive walk — this is what
    `path`-as-ids buys, and what makes trashing or purging a whole branch a
    bounded operation instead of a query per node.
    """
    manager = Folder.all_objects if include_trashed else Folder.objects
    return manager.filter(user_id=folder.user_id, path__startswith=folder.path)


def breadcrumbs(folder: Folder | None) -> list[dict]:
    """Root-first trail of `{id, name}` for `folder`, excluding itself.

    Root is represented by the empty list, not by a synthetic entry — clients
    render their own label for it (the web app says "My Files").
    """
    if folder is None:
        return []
    ancestor_ids = folder.ancestor_ids
    if not ancestor_ids:
        return []
    by_id = {
        f.pk: f for f in Folder.all_objects.filter(
            pk__in=ancestor_ids, user_id=folder.user_id,
        )
    }
    return [
        {'id': pk, 'name': by_id[pk].name}
        for pk in ancestor_ids if pk in by_id
    ]


def name_path(folder: Folder | None) -> str:
    """Human-readable `/Reports/2026` for display only.

    Derived on read, never stored: a stored name path is the thing that makes
    rename O(descendants) and tempts someone into accepting it as a locator.
    """
    if folder is None:
        return '/'
    return '/' + '/'.join([c['name'] for c in breadcrumbs(folder)] + [folder.name])


# ---------------------------------------------------------------------------
# Writing the tree
# ---------------------------------------------------------------------------

def validate_name(name: str) -> str:
    cleaned = (name or '').strip()
    if not cleaned:
        raise FilesystemError('A folder needs a name.')
    if len(cleaned) > 255:
        raise FilesystemError('That name is too long (255 characters max).')
    if cleaned in ('.', '..'):
        raise FilesystemError('That name is reserved.')
    if _ILLEGAL_NAME.search(cleaned):
        raise FilesystemError('A folder name cannot contain slashes or control characters.')
    return cleaned


def unique_name(user, parent: Folder | None, name: str, *, exclude_pk=None) -> str:
    """`name`, or `name (2)` / `name (3)` … if a live sibling already has it.

    Used only where refusing would be hostile — restoring from trash, where
    the user did not choose the moment and a name clash is not their doing.
    Creating and renaming *reject* duplicates instead, because there the name
    is a deliberate choice and silently altering it hides a mistake.
    """
    taken = set(
        Folder.objects
        .filter(user=user, parent=parent)
        .exclude(pk=exclude_pk)
        .values_list('name', flat=True)
    )
    if name not in taken:
        return name
    match = _SUFFIXED.match(name)
    stem = match.group('stem') if match else name
    n = int(match.group('n')) + 1 if match else 2
    while f'{stem} ({n})' in taken:
        n += 1
    return f'{stem} ({n})'


def create_folder(user, name: str, parent: Folder | None) -> Folder:
    """Create one folder. `parent` must already have come from `resolve_folder`."""
    name = validate_name(name)

    # depth is 0-based, so a folder at depth MAX-1 is the deepest allowed.
    if parent is not None and parent.depth + 1 > MAX_FOLDER_DEPTH - 1:
        raise FilesystemError(f'Folders cannot nest deeper than {MAX_FOLDER_DEPTH} levels.')
    if Folder.objects.filter(user=user).count() >= MAX_FOLDERS_PER_USER:
        raise FilesystemError(f'You have reached the limit of {MAX_FOLDERS_PER_USER} folders.')
    if Folder.objects.filter(user=user, parent=parent, name=name).exists():
        where = f'"{parent.name}"' if parent else 'your root'
        raise FilesystemError(f'{where} already contains a folder called "{name}".')

    return Folder.objects.create(user=user, parent=parent, name=name)


def rename_folder(folder: Folder, name: str) -> Folder:
    """Rename in place. One column write — descendants are untouched, because
    `path` holds ids and none of them changed."""
    name = validate_name(name)
    if name == folder.name:
        return folder
    if Folder.objects.filter(
        user_id=folder.user_id, parent_id=folder.parent_id, name=name,
    ).exclude(pk=folder.pk).exists():
        raise FilesystemError(f'A folder called "{name}" is already there.')
    folder.name = name
    folder.save(update_fields=['name', 'updated_at'])
    return folder


def _subtree_height(folder: Folder) -> int:
    """Levels below `folder` (0 when it is a leaf)."""
    deepest = subtree(folder).order_by('-depth').values_list('depth', flat=True).first()
    return (deepest or folder.depth) - folder.depth


@transaction.atomic
def move(user, *, folders: Sequence[Folder] = (), documents: Sequence[Document] = (),
         target: Folder | None) -> dict:
    """Reparent folders and/or documents under `target` (None = root).

    Everything here has already been through `resolve_folder`/`resolve_folders`,
    so ownership is settled before this runs; what is left is shape — cycles and
    depth.

    Note what this function does *not* touch: `knowledge_base`, `status`,
    `chunk_count`, or any byte on disk. A move is a column write. The module
    does not import `inference.tasks` at all, so re-indexing on move is not
    something a future edit here can do by accident.
    """
    for folder in folders:
        if target is not None:
            if target.pk == folder.pk:
                raise FilesystemError('A folder cannot be moved into itself.')
            # `path` is self-inclusive, so a descendant's path starts with the
            # ancestor's. Two string comparisons, no queries, no recursion.
            if target.path.startswith(folder.path):
                raise FilesystemError(
                    'A folder cannot be moved into its own descendant.'
                )
            if target.depth + 1 + _subtree_height(folder) > MAX_FOLDER_DEPTH - 1:
                raise FilesystemError(
                    f'That move would nest deeper than {MAX_FOLDER_DEPTH} levels.'
                )
        if folder.parent_id == (target.pk if target else None):
            continue
        if Folder.objects.filter(
            user=user, parent=target, name=folder.name,
        ).exclude(pk=folder.pk).exists():
            where = f'"{target.name}"' if target else 'your root'
            raise FilesystemError(f'{where} already contains a folder called "{folder.name}".')

        old_path, old_depth = folder.path, folder.depth

        # The descendants have to be identified *before* the save. Afterwards
        # `folder.path` is the new value while the descendants still carry the
        # old prefix, so `subtree(folder)` would match nothing and the rewrite
        # below would silently do nothing at all.
        descendant_ids = list(
            Folder.all_objects
            .filter(user_id=folder.user_id, path__startswith=old_path)
            .exclude(pk=folder.pk)
            .values_list('pk', flat=True)
        )

        folder.parent = target
        folder.save()                      # recomputes its own path and depth
        delta = folder.depth - old_depth

        if descendant_ids:
            # One bounded UPDATE for the whole subtree. A Python walk here
            # would be a query per descendant inside a write transaction.
            Folder.all_objects.filter(pk__in=descendant_ids).update(
                path=Replace('path', Value(old_path), Value(folder.path)),
                depth=F('depth') + delta,
                updated_at=timezone.now(),
            )

    if documents:
        Document.objects.filter(
            user=user, pk__in=[d.pk for d in documents],
        ).update(folder=target, updated_at=timezone.now())

    return {
        'moved_folders': len(folders),
        'moved_documents': len(documents),
        'target_folder_id': target.pk if target else None,
    }
