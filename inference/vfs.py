"""
The agent's virtual filesystem: POSIX-shaped paths over the user's own tree.

An agent needs a filesystem it can *plan* with — `/notes/draft.md`, "list what
is in there", "write that to a file" — and this platform already has durable
per-user storage in `Folder` + `Document`. This module is the adapter between
the two, and it is deliberately **artificial**: `os` is never imported, nothing
here opens a handle, and no path an agent types corresponds to any path on any
host. The worst a traversal bug can reach is another row, never another file,
and never the process's own disk.

Three properties carry the isolation, and each is structural rather than
checked:

*Paths are walked, never matched.* A path becomes a list of name segments,
resolved one parent/child hop at a time from the scope root through
`filesystem.child_by_name`. No query anywhere joins a caller-supplied string to
`Folder.path`, so there is no string that addresses a row outside the subtree
the walk started in — every step is an edge from a folder already resolved to
its own children. This is why `filesystem.py`'s "no route accepts a path as a
locator" survives a module whose entire API is paths.

*`..` is clamped, not refused.* Popping an empty stack is a no-op. Refusing the
segment outright would look stricter and behave worse: models emit `../`
constantly, and an error teaches the model to retry with another spelling of
the same intent, while a clamp gives it exactly what a chroot would have.

*The scope root is a folder the user owns.* Every operation starts from a
`Folder` that came out of `resolve_folder`, or from `None`, which *is* the
user's root and is unforgeable. `mode` narrows what may be done inside that
subtree; it can never widen where the subtree is.

Writing is narrowed a second time. For most modes the readable and writable
subtrees are the same, so the walk alone confines both. `read_all_write_own`
reads the whole tree and writes only the agent's own home, and that gap is held
by `FileScope.write_prefix` — a segment prefix checked on every write, because
a walk that starts at the tree root cannot confine anything by itself.

What this module does not do is index. A written file is `status='stored'` —
the raw-backend terminal state, browsable and readable but not searchable — and
no knowledge base is touched. "Folders organise, KBs index" holds for agents
exactly as it does for people, so writing a file is never a silent embedding
bill. Deleting goes through `recycle.trash`, so an agent's mistake lands in the
user's recycle bin with the retention window every other delete gets, rather
than being unrecoverable.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Any, Sequence

from workflow_backend.thresholds import (
    AGENT_FILE_LIST_LIMIT,
    AGENT_FILE_READ_CHARS,
    AGENT_FILE_WRITE_CHARS,
    AGENT_HOME_ROOT,
    CHAT_HOME_ROOT,
)

from . import filesystem as fs
from .models import Document, Folder

logger = logging.getLogger(__name__)

#: The `SubAgent.sandbox['fileAccess']` values that grant anything at all.
#: `none` is absent on purpose — it resolves to no scope, and therefore to no
#: tools being offered rather than to tools that refuse.
READONLY = 'readonly'
SCOPED = 'scoped'
FULL = 'full'
#: Read the user's whole tree, write only inside the agent's own home. The one
#: setting where the readable and writable subtrees differ — see `FileScope`.
READ_ALL_WRITE_OWN = 'read_all_write_own'

#: Characters that cannot appear in a name, because they would either fake a
#: hierarchy that is not there (`/`) or corrupt the listing (`\x00-\x1f`). Same
#: set `filesystem._ILLEGAL_NAME` enforces for folders, applied here to
#: *document* names too — a document called "a/b" would be unaddressable by the
#: only API that can reach it.
_ILLEGAL = re.compile(r'[/\\\x00-\x1f]')

#: Extension -> `Document.FILE_TYPE_CHOICES`. Anything unrecognised is stored as
#: text, which is what it is: the content column is a TextField either way, so
#: the type is a display hint and guessing wrong costs an icon, not data.
_EXT_TO_TYPE = {
    'md': 'md', 'markdown': 'md',
    'txt': 'txt', 'text': 'txt', 'log': 'txt',
    'json': 'json',
    'csv': 'csv',
    'html': 'html', 'htm': 'html',
}


class VfsError(Exception):
    """Anything the model should read and correct: a path that does not exist,
    a write into a read-only scope, a file over the size cap. Tools render the
    message verbatim, so it is written to be read by a model — it says what was
    refused and what to do instead, never just "invalid"."""


# ---------------------------------------------------------------------------
# Scope — what one run may see
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FileScope:
    """The subtree one agent run may address, and what it may do inside it.

    Readable and writable are **two subtrees, not one subtree and a flag**.
    For three of the four modes they coincide, and confinement comes for free
    from the walk: every path is resolved hop-by-hop from `root`, so no string
    addresses anything outside it and a write needs no further check. The
    fourth mode, `read_all_write_own`, deliberately breaks that identity — it
    reads the whole tree and writes only the agent's own home — so writes need
    a check the walk cannot give them.

    That check is `write_prefix`: the segments, *relative to `root`*, that a
    write must begin with. Comparing segments rather than resolved rows is what
    makes it work for paths that do not exist yet, which `write_file` needs
    because it creates missing parents before there is any row to compare.
    """

    user: Any
    #: `None` is the user's root. Not a sentinel for "unset" — see
    #: `filesystem.resolve_folder`, which uses the same convention.
    root: Folder | None
    mode: str
    #: How the root is described to the model, so a path in an error message
    #: means something to whoever reads the transcript.
    label: str
    #: Segments a write must start with. `()` means anywhere inside `root`;
    #: `None` means nothing is writable at all. Note `()` and `None` are the
    #: distinction the old boolean carried, kept apart here because an empty
    #: tuple is a real prefix that everything matches.
    write_prefix: tuple[str, ...] | None = None
    #: How the writable subtree is described to the model. Equal to `label`
    #: wherever the two subtrees coincide.
    write_label: str = ''

    #: A *second* writable subtree: the delegating agent's home, granted to a
    #: worker for the length of a delegated run. `None` for every other scope.
    #:
    #: Named rather than folded into a list of prefixes, because the two are
    #: not interchangeable — one is "your own files", the other is "the folder
    #: you share with whoever asked", and both the system prompt and
    #: `list_dir`'s per-directory `writable` flag need to say which is which. A
    #: list would make them the same thing with different indices.
    #:
    #: It is what lets delegation hand back *paths* instead of prose. Without
    #: it a worker's findings could only come back through the transcript, so
    #: every fan-out paid its whole result into the parent's context window —
    #: and anything over the cap was archived and had to be fetched back out
    #: again. A shared folder makes the answer "wrote findings-2.md", and the
    #: parent reads it only if it needs to.
    shared_prefix: tuple[str, ...] | None = None
    shared_label: str = ''

    @property
    def writable(self) -> bool:
        """Whether anything at all may be written in this scope."""
        return self.write_prefix is not None or self.shared_prefix is not None

    def may_write_at(self, parts: Sequence[str]) -> bool:
        """Whether a write at `parts` (scope-relative) lands in a writable subtree.

        A path shorter than a prefix is *not* writable: standing at `/` with a
        prefix of `Agents/Reporter` means the agent may not create siblings of
        its own home, only descend into it.

        Both subtrees are checked because a worker legitimately has two — its
        own home and the shared workspace — and confinement still comes from
        the walk: each prefix names segments under a root that was resolved
        from a row the user owns, so neither one widens *where* the scope can
        reach, only what may be written once it is there.
        """
        for prefix in (self.write_prefix, self.shared_prefix):
            if prefix is not None and tuple(parts[:len(prefix)]) == prefix:
                return True
        return False


def build_scope(user, file_access: str, *, agent_name: str = '') -> FileScope | None:
    """The scope implied by an agent's `fileAccess` setting, or None for `none`.

    None means the caller offers no file tools at all, which is the same rule
    `ask_vision` follows: an advertised tool that cannot run is worse than one
    never offered, because the model plans around it and then has to explain
    the failure.

    The four served modes are two independent questions — what may be read, and
    what may be written — which happen to coincide in three of them:

    * `readonly` — reads the whole tree, writes nothing.
    * `scoped`   — reads and writes only the agent's own home.
    * `full`     — reads and writes the whole tree.
    * `read_all_write_own` — reads the whole tree, writes only its own home.

    The last is the useful default in practice and the reason `write_prefix`
    exists: an agent asked to summarise the user's documents needs to read
    outside its home, and an agent that can then write anywhere is a much
    larger grant than the task called for.

    Both `scoped` and `read_all_write_own` need the home folder to exist, so
    this is where it is created.
    """
    mode = (file_access or '').strip().lower()
    if mode not in (READONLY, SCOPED, FULL, READ_ALL_WRITE_OWN):
        return None

    if mode == READONLY:
        return FileScope(user=user, root=None, mode=mode, label='/',
                         write_prefix=None, write_label='')

    if mode == FULL:
        return FileScope(user=user, root=None, mode=mode, label='/',
                         write_prefix=(), write_label='/')

    home = agent_home(user, agent_name)
    home_label = f'/{AGENT_HOME_ROOT}/{home.name}'

    if mode == SCOPED:
        # Root *at* the home, so the walk itself is the confinement and the
        # agent cannot even see the rest of the tree.
        return FileScope(user=user, root=home, mode=mode, label=home_label,
                         write_prefix=(), write_label=home_label)

    # read_all_write_own: root at the whole tree for reading, and a prefix that
    # pins writes back to the home. The prefix is expressed in the same exact,
    # case-sensitive segment names `filesystem.child_by_name` matches on, so
    # what the prefix admits and what the walk resolves cannot disagree.
    return FileScope(
        user=user, root=None, mode=mode, label='/',
        write_prefix=(AGENT_HOME_ROOT, home.name), write_label=home_label,
    )


def with_shared_workspace(
    scope: FileScope | None,
    prefix: Sequence[str],
    label: str = '',
) -> FileScope | None:
    """Grant `scope` write access to a second subtree — the caller's own folder.

    This is what lets a worker hand its parent a *path* instead of prose. The
    parent already writes in that folder, so a worker that can write there too
    has somewhere to leave a report the parent reads on demand rather than pays
    for in its context window.

    `prefix` is the delegating scope's own `write_prefix`, passed straight
    through rather than rebuilt from an agent name. Deriving it from the caller
    is what keeps the two halves from disagreeing: the shared folder is *by
    definition* the one the parent writes to, so there is no second rule that
    could drift — and it works unchanged when the delegator is chat, whose
    folder is `/Chat/` and not an agent home at all.

    **It only ever adds, and only where it can mean something.** Four cases are
    left exactly as they were, each for its own reason:

    * a worker rooted at its own home (`fileAccess='scoped'`) — its
      configuration says "confine me to my folder", and re-rooting it would
      make one brief write to different places depending on who called it;
    * a `readonly` worker — its configuration says it writes nothing, and
      being delegated to is not consent to change that;
    * an empty `prefix` — a parent that writes everywhere (`full`, chat's own
      root) has no particular folder to share, and `()` would match every path,
      quietly upgrading the worker to write anywhere;
    * a prefix the worker already writes to — nothing to add.

    Returns the scope unchanged in all of them, so a caller never has to ask
    whether it applied: a worker without a workspace answers the way it always
    did.
    """
    if scope is None:
        return scope
    prefix = tuple(str(p) for p in (prefix or ()) if str(p))
    if not prefix:
        return scope
    # Rooted somewhere other than the tree: the walk starts inside the agent's
    # own home and can never address the parent's, so a prefix here would name
    # segments no path can reach.
    if scope.root is not None:
        return scope
    if scope.write_prefix is None or prefix == scope.write_prefix:
        return scope

    return replace(
        scope,
        shared_prefix=prefix,
        shared_label=label or '/' + '/'.join(prefix),
    )


def chat_scope(user) -> FileScope:
    """What a chat turn may address: read the whole tree, write into `/Chat/`.

    Chat had no scope at all until now, which is why `requires="files"` was an
    unconditional `False` — there was no `fileAccess` setting to build one
    from, so the tools were withheld rather than offered and refused. That was
    the right call while the only file tools reached the *host* filesystem.
    These reach rows in the user's own tree, so the question is no longer
    whether chat may touch files but which ones, and that has a fixed answer
    rather than a configurable one.

    `read_all_write_own` is that answer, and it is the same shape an agent gets
    from the same words. Reading the whole tree is what makes "summarise my
    notes" work at all, and it grants nothing new — the KB tools already read
    the user's documents. Writing is confined to one visible folder, so a
    conversation cannot scatter files through a tree the user organised, and
    everything chat produced is in one place they can find, move or delete.

    There is deliberately no folder per session. A user looking for the summary
    they asked for yesterday should find it under `/Chat/`, not under a uuid
    they never saw; and a conversation that cannot read what an earlier one
    wrote is a filing cabinet that forgets.
    """
    fs.ensure_folder(user, CHAT_HOME_ROOT, None)
    return FileScope(
        user=user, root=None, mode=READ_ALL_WRITE_OWN, label='/',
        write_prefix=(CHAT_HOME_ROOT,), write_label=f'/{CHAT_HOME_ROOT}',
    )


def agent_home(user, agent_name: str) -> Folder:
    """`/Agents/<agent name>/`, created on demand.

    Keyed by **agent, not by run**, and that is the one place this design
    departs from how an ephemeral sandbox works. A run is the right unit for
    isolating *compute* and the wrong one for a workspace: per-run folders would
    leave one directory per execution in the user's own tree — a mess they did
    not ask to clean up — and would make "read what you wrote last time"
    impossible without inventing a second mechanism to carry it across. This
    tree is the durable half on purpose. When there is a real ephemeral
    workspace for compute, it belongs beside this, not instead of it.

    Under a real folder rather than a hidden one, because these files are the
    user's: a tree they cannot see in the UI is a tree they cannot clean up.
    """
    parent = fs.ensure_folder(user, AGENT_HOME_ROOT, None)
    return fs.ensure_folder(user, safe_name(agent_name) or 'agent', parent)


def safe_name(name: str) -> str:
    """A caller-supplied name reduced to something storable.

    Agent names and model-authored filenames both land here. Separators become
    hyphens rather than being rejected: the model meant a name, and the useful
    answer is to store the name it meant.
    """
    cleaned = _ILLEGAL.sub('-', (name or '').strip())
    if cleaned in ('.', '..'):
        cleaned = ''
    return cleaned[:255]


# ---------------------------------------------------------------------------
# Path resolution — the walk
# ---------------------------------------------------------------------------

def segments(path: str) -> list[str]:
    """Split a path into resolved name segments, relative to the scope root.

    `.` drops, `..` pops, and popping an empty stack is a no-op — the clamp
    that makes escape impossible without making `..` an error. Backslashes are
    read as separators too, since a model that has seen Windows paths will
    write them and no name may contain one anyway.
    """
    out: list[str] = []
    for raw in (path or '').replace('\\', '/').split('/'):
        seg = raw.strip()
        if not seg or seg == '.':
            continue
        if seg == '..':
            if out:
                out.pop()
            continue
        out.append(seg)
    return out


def render(scope: FileScope, parts: Sequence[str]) -> str:
    """A scope-relative path as the model should see it, for messages."""
    tail = '/'.join(parts)
    base = scope.label.rstrip('/')
    return f'{base}/{tail}' if tail else (base or '/')


def _folder_at(scope: FileScope, parts: Sequence[str]) -> Folder | None:
    """Walk `parts` from the scope root. Raises if any segment is missing."""
    node = scope.root
    for i, seg in enumerate(parts):
        child = fs.child_by_name(scope.user, node, seg)
        if child is None:
            raise VfsError(
                f'No such directory: {render(scope, parts[:i + 1])}. '
                f'List the parent first to see what is actually there.'
            )
        node = child
    return node


def _document_in(scope: FileScope, folder: Folder | None, name: str) -> Document | None:
    """One live document by exact name in `folder`.

    `Document.objects` is the `LiveManager`, so a trashed file is not found —
    which is what makes "delete then write the same name" behave the way the
    model expects instead of colliding with a row it cannot see.
    """
    return Document.objects.filter(user=scope.user, folder=folder, name=name).first()


def _split_leaf(scope: FileScope, path: str) -> tuple[list[str], str]:
    """`parts-to-the-parent, leaf name`, refusing a path that names the root."""
    parts = segments(path)
    if not parts:
        raise VfsError(
            f'That path names the directory {scope.label}, not a file. '
            f'Give a file name, like {scope.label.rstrip("/")}/notes.md.'
        )
    return parts[:-1], parts[-1]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def list_dir(scope: FileScope, path: str = '/') -> dict:
    """Directories and files directly inside `path`."""
    parts = segments(path)
    folder = _folder_at(scope, parts)

    dirs = list(fs.children(scope.user, folder)[: AGENT_FILE_LIST_LIMIT + 1])
    docs = list(
        Document.objects
        .filter(user=scope.user, folder=folder)
        .order_by('name')
        .values('id', 'name', 'file_type', 'file_size', 'updated_at')
        [: AGENT_FILE_LIST_LIMIT + 1]
    )

    truncated = len(dirs) > AGENT_FILE_LIST_LIMIT or len(docs) > AGENT_FILE_LIST_LIMIT
    dirs = dirs[:AGENT_FILE_LIST_LIMIT]
    docs = docs[:AGENT_FILE_LIST_LIMIT]

    return {
        'path': render(scope, parts),
        # Per-directory, not per-scope: under `read_all_write_own` most of the
        # tree is readable and unwritable, and a flat `scope.writable` would
        # tell the model it may write in every folder it lists — which it would
        # believe, and plan around, right up until the write was refused.
        'writable': scope.may_write_at(parts),
        'directories': [f'{d.name}/' for d in dirs],
        'files': [
            {
                'name': d['name'],
                'type': d['file_type'],
                'bytes': d['file_size'],
                'modified': d['updated_at'].isoformat() if d['updated_at'] else None,
            }
            for d in docs
        ],
        # A capped listing and a complete one must not look alike — the same
        # rule the HTTP list endpoints follow.
        'truncated': truncated,
        **({'note': f'Listing capped at {AGENT_FILE_LIST_LIMIT} entries per kind.'}
           if truncated else {}),
    }


def read_file(scope: FileScope, path: str, *, offset: int = 0,
              limit: int | None = None) -> dict:
    """Text of one file, from `offset`, capped at `AGENT_FILE_READ_CHARS`."""
    parent_parts, name = _split_leaf(scope, path)
    folder = _folder_at(scope, parent_parts)
    doc = _document_in(scope, folder, name)
    if doc is None:
        raise VfsError(
            f'No such file: {render(scope, parent_parts + [name])}. '
            f'List the directory to see what is there.'
        )

    cap = min(limit or AGENT_FILE_READ_CHARS, AGENT_FILE_READ_CHARS)
    body = doc.content_text or ''
    offset = max(0, int(offset or 0))
    window = body[offset:offset + cap]
    end = offset + len(window)
    remaining = max(0, len(body) - end)

    out = {
        'path': render(scope, parent_parts + [name]),
        'document_id': doc.id,
        'content': window,
        'offset': offset,
        'chars': len(window),
        'total_chars': len(body),
    }
    if remaining:
        # Named, not silent. A truncated read that looks complete is how a model
        # ends up summarising half a document as if it were the whole one.
        out['truncated'] = True
        out['note'] = (
            f'{remaining:,} characters remain — call read_file again with '
            f'offset={end} to continue.'
        )
    if not body:
        out['note'] = (
            'This file has no extracted text. It may be a binary upload (PDF, '
            'image) that was never processed, rather than an empty file.'
        )
    return out


def write_file(scope: FileScope, path: str, content: str, *,
               append: bool = False) -> dict:
    """Create or overwrite one file, creating parent directories as needed.

    `mkdir -p` semantics deliberately: a model that has to create three folders
    before it can save a file will spend three tool calls doing it and get one
    of them wrong. The directories it creates are ordinary folders the user can
    see, move and delete.
    """
    parent_parts, raw_name = _split_leaf(scope, path)
    # Checked before `_make_dirs`, not after: creating three folders and *then*
    # refusing the file would leave the tree littered with directories from a
    # write that never happened.
    _require_write_at(scope, parent_parts, 'write')

    name = safe_name(raw_name)
    if not name:
        raise VfsError(f'"{raw_name}" is not a usable file name.')

    text = content if isinstance(content, str) else str(content)
    if len(text) > AGENT_FILE_WRITE_CHARS:
        raise VfsError(
            f'That is {len(text):,} characters; the limit for one write is '
            f'{AGENT_FILE_WRITE_CHARS:,}. Write it in parts, or write less.'
        )

    folder = _make_dirs(scope, parent_parts)
    doc = _document_in(scope, folder, name)

    if doc is None:
        doc = Document.objects.create(
            user=scope.user,
            folder=folder,
            name=name,
            file='',                       # no upload backs this; the text is the file
            file_type=_file_type(name),
            file_size=len(text.encode('utf-8')),
            content_text=text,
            # 'stored' is the raw-backend terminal state: readable and
            # browsable, never indexed. An agent writing a file must not
            # silently start an embedding job.
            status='stored',
            metadata={'created_by': 'agent'},
        )
        created = True
    else:
        text = (doc.content_text or '') + text if append else text
        if len(text) > AGENT_FILE_WRITE_CHARS:
            raise VfsError(
                f'Appending would make this file {len(text):,} characters, over '
                f'the {AGENT_FILE_WRITE_CHARS:,} limit.'
            )
        doc.content_text = text
        doc.file_size = len(text.encode('utf-8'))
        doc.save(update_fields=['content_text', 'file_size', 'updated_at'])
        created = False

    return {
        'path': render(scope, parent_parts + [name]),
        'document_id': doc.id,
        'created': created,
        'appended': append and not created,
        'chars': len(text),
    }


def edit_file(scope: FileScope, path: str, old_text: str, new_text: str,
              *, replace_all: bool = False) -> dict:
    """Replace an exact run of text inside one file, leaving the rest untouched.

    The alternative was the only thing on offer: `write_file`, which replaces
    the whole document. Changing one line of a long note therefore meant
    reading every character in and emitting every character back out — two full
    payloads billed per edit, and the model silently dropping the parts it
    decided to paraphrase on the way through. The longer the file, the more
    likely an edit was to quietly lose something.

    So the contract is exact-match-or-refuse, and it is deliberately strict in
    both directions:

    * text that is not present is an error, never a no-op reported as success —
      a model told "done" about an edit that changed nothing will build its
      next three steps on a file it believes it has already fixed;
    * text that appears more than once is an error too, unless `replace_all`
      says the caller meant all of them. Silently taking the first occurrence
      is how an edit lands in the wrong paragraph, and nothing downstream can
      detect that it did.

    Both refusals name the count, because the model's next move differs: zero
    means re-read the file, several means include more surrounding text.
    """
    if not old_text:
        raise VfsError(
            'old_text is empty. Give the exact text to replace — to create a '
            'file or overwrite one entirely, use write_file instead.'
        )

    parent_parts, name = _split_leaf(scope, path)
    # Checked before the file is even looked up, so a read-only scope gets the
    # same answer for a file that exists and one that does not — an editable
    # location is not something to probe for.
    _require_write_at(scope, parent_parts, 'edit')

    folder = _folder_at(scope, parent_parts)
    doc = _document_in(scope, folder, name)
    if doc is None:
        raise VfsError(
            f'No such file: {render(scope, parent_parts + [name])}. '
            f'List the directory to see what is there.'
        )

    body = doc.content_text or ''
    if not body:
        raise VfsError(
            f'{render(scope, parent_parts + [name])} has no text to edit. It '
            f'may be a binary upload (PDF, image) rather than an empty file.'
        )

    found = body.count(old_text)
    if found == 0:
        raise VfsError(
            'That exact text is not in the file. Read it again and copy the '
            'text to replace verbatim — whitespace and line breaks included.'
        )
    if found > 1 and not replace_all:
        raise VfsError(
            f'That text appears {found} times. Include more of the surrounding '
            f'text so it matches once, or pass replace_all=true to change all '
            f'{found}.'
        )

    updated = body.replace(old_text, new_text) if replace_all else \
        body.replace(old_text, new_text, 1)

    # The same ceiling a write is held to. An edit is a write; a growing
    # replacement is the one way it can push a file past the cap.
    if len(updated) > AGENT_FILE_WRITE_CHARS:
        raise VfsError(
            f'That edit would make this file {len(updated):,} characters, over '
            f'the {AGENT_FILE_WRITE_CHARS:,} limit.'
        )

    doc.content_text = updated
    doc.file_size = len(updated.encode('utf-8'))
    doc.save(update_fields=['content_text', 'file_size', 'updated_at'])

    return {
        'path': render(scope, parent_parts + [name]),
        'document_id': doc.id,
        'replacements': found if replace_all else 1,
        'chars': len(updated),
        'chars_before': len(body),
    }


def find(scope: FileScope, query: str, *, limit: int = 0) -> dict:
    """Locate files by name or by the text inside them, anywhere in scope.

    A filesystem an agent cannot search is a filing cabinet with no index: with
    only `list_files` and `read_file`, finding last month's report means walking
    the tree a directory at a time and reading candidates in full, which costs
    a tool call per guess and usually ends in the model giving up and asking the
    user where they put it.

    This is deliberately **not** knowledge-base search, and the two must not be
    confused. A KB answers "what does the corpus say about X" and costs an
    embedding job to build; this answers "which file is called X, or mentions
    it" and costs one indexed `LIKE`. Keeping them apart is what lets writing a
    file stay free — the rule this module opens by stating, that folders
    organise and KBs index, holds precisely because finding a file never needed
    an index in the first place.

    Substring matching, not ranking. `icontains` on a name and on the stored
    text is honest about what it does and cannot pretend to relevance it has
    not computed; a model given a ranked list of near-misses treats the top one
    as the answer, while a model given "three files contain this string" goes
    and reads them.
    """
    needle = (query or '').strip()
    if len(needle) < 2:
        raise VfsError('Give at least two characters to search for.')

    cap = min(limit or AGENT_FILE_LIST_LIMIT, AGENT_FILE_LIST_LIMIT)

    rows = Document.objects.filter(user=scope.user)
    if scope.root is not None:
        # Confined by the *resolved* root's id path, never by a caller string —
        # the walk rule this module opens with is about paths a model typed,
        # and this prefix comes from a row we already own. One indexed match,
        # which is what `Folder.path` holding ids is for.
        rows = rows.filter(folder__in=fs.subtree(scope.root, include_trashed=False))

    from django.db.models import Q

    rows = rows.filter(Q(name__icontains=needle) | Q(content_text__icontains=needle))
    rows = rows.select_related('folder').order_by('-updated_at')

    # One extra row is the difference between "there are exactly `cap` matches"
    # and "there are more"; without it a full page and a complete result are
    # indistinguishable and `truncated` would be a guess.
    window = list(rows[:cap + 1])
    truncated = len(window) > cap

    # Resolved per folder, not per document: a search returns many files from
    # few directories, and `breadcrumbs` is a query each. Bounded either way,
    # but 200 results in one folder should cost one lookup, not 200.
    parts_by_folder: dict[Any, list[str]] = {}

    found = []
    for doc in window[:cap]:
        key = doc.folder_id
        if key not in parts_by_folder:
            parts_by_folder[key] = _folder_parts(scope, doc.folder)
        parts = parts_by_folder[key]
        found.append({
            'path': render(scope, parts + [doc.name]),
            'document_id': doc.id,
            'chars': len(doc.content_text or ''),
            'in_name': needle.lower() in (doc.name or '').lower(),
            'writable': scope.may_write_at(parts),
        })

    out = {'query': needle, 'matches': found, 'count': len(found)}
    if truncated:
        # Named rather than silent, for the same reason `read_file` names a
        # truncated read: a capped list and a complete one must not look alike,
        # or the model concludes the file it wanted does not exist.
        out['truncated'] = True
        out['note'] = (
            f'Showing the {cap} most recently updated matches. Narrow the '
            f'search if what you want is not here.'
        )
    if not found:
        out['note'] = 'No file in scope has that in its name or its text.'
    return out


def _folder_parts(scope: FileScope, folder: Folder | None) -> list[str]:
    """A folder's name segments relative to the scope root.

    A file reached by search has to describe itself the same way a file reached
    by a walk does — same path, same answer to "may I write here?" — or the two
    routes to one document would disagree about it.

    Built from `breadcrumbs`, which resolves a whole ancestor chain in one
    query off `Folder.ancestor_ids`, rather than by following `parent` upward a
    row at a time.
    """
    if folder is None:
        return []
    names = [c['name'] for c in fs.breadcrumbs(folder)] + [folder.name]
    if scope.root is None:
        return names
    root_names = [c['name'] for c in fs.breadcrumbs(scope.root)] + [scope.root.name]
    return names[len(root_names):]


def make_dir(scope: FileScope, path: str) -> dict:
    """Create a directory and any missing parents. Idempotent."""
    parts = segments(path)
    if not parts:
        raise VfsError('Give a directory name to create.')
    _require_write_at(scope, parts, 'create directories')
    _make_dirs(scope, parts)
    return {'path': render(scope, parts), 'created': True}


def delete(scope: FileScope, path: str) -> dict:
    """Move one file or directory to the user's recycle bin.

    Trash, never purge. An agent deleting the wrong thing is a mistake the user
    must be able to undo, and `recycle.trash` is the same door their own delete
    goes through — so it gets the same retention window, drops the same vectors,
    and shows up in the same trash view.
    """
    parts = segments(path)
    if not parts:
        raise VfsError(f'Refusing to delete {scope.label} itself.')
    _require_write_at(scope, parts, 'delete')

    from . import recycle

    parent = _folder_at(scope, parts[:-1])
    leaf = parts[-1]

    doc = _document_in(scope, parent, leaf)
    if doc is not None:
        recycle.trash(scope.user, documents=[doc])
        return {'path': render(scope, parts), 'deleted': 'file',
                'restorable': True}

    folder = fs.child_by_name(scope.user, parent, leaf)
    if folder is not None:
        recycle.trash(scope.user, folders=[folder])
        return {'path': render(scope, parts), 'deleted': 'directory',
                'restorable': True}

    raise VfsError(f'Nothing to delete at {render(scope, parts)}.')


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _require_write_at(scope: FileScope, parts: Sequence[str], verb: str) -> None:
    """Refuse a write this scope does not allow at this path.

    Path-aware rather than a bare boolean, because `read_all_write_own` reads a
    wider subtree than it writes: the walk confines reads, and this confines
    writes. The two refusals are worded differently on purpose — "read-only"
    and "not there, but here" call for different next moves from the model, and
    a single message would send it retrying the wrong one.
    """
    if not scope.writable:
        raise VfsError(
            f'This agent has read-only file access, so it cannot {verb}. '
            f'Report what you would have written instead of retrying.'
        )
    if not scope.may_write_at(parts):
        raise VfsError(
            f'This agent can read anywhere in your files but can only {verb} '
            f'inside {scope.write_label}. Retry with a path under '
            f'{scope.write_label}/ — the rest of the tree is readable, not writable.'
        )


def _make_dirs(scope: FileScope, parts: Sequence[str]) -> Folder | None:
    """Walk `parts`, creating what is missing. Returns the deepest folder."""
    node = scope.root
    for raw in parts:
        name = safe_name(raw)
        if not name:
            raise VfsError(f'"{raw}" is not a usable directory name.')
        try:
            node = fs.ensure_folder(scope.user, name, node)
        except fs.FilesystemError as e:
            # A depth or per-user folder cap. Surfaced verbatim: the message
            # already names the limit, and the model can act on it.
            raise VfsError(str(e)) from e
    return node


def _file_type(name: str) -> str:
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    return _EXT_TO_TYPE.get(ext, 'txt')
