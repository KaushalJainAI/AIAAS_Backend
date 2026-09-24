"""
The fake world an eval runs in (`docs/EVAL_ENVIRONMENTS_PLAN.md`).

An `EvalEnvironment` is built per attempt and handed to `run_agent` as
`environment=`. It does three things, each where the equivalent real thing
happens today:

1. **Scope: what the agent can see.** `attempt_scope()` roots the file tools
   at the attempt folder, whatever the agent's `fileAccess` says; `kb_id`
   (E-2) is the world's hidden KB and nothing else. The owner's real tree is
   unreachable during an eval, not merely unlikely to be read.
2. **Dispatch: where tool calls go.** `AgentToolbox.dispatch` asks
   `simulates(name)` first. A simulated tool is answered by the simulator; a
   real tool with no simulator is **withheld** from `descriptors` and refused
   at dispatch, so an eval can never reach a real service (fail closed).
3. **State: fresh every attempt, snapshot afterwards.** `prepare()` resets
   every surface; `snapshot_files()` / `snapshot_env()` produce what the
   state graders read (`GradeContext.files`, `GradeContext.env`) and the
   "what changed" panel (`EvalResult.env_changes`).

Surfaces and their phases: files (E-1, real vfs rows, no simulator needed),
KB (E-2, real hidden KB rows), mail + calendar (E-3, `eval/sim/`), drive +
web (E-4, `eval/sim/`). Anything without a surface here — MCP tools, talk,
data, API, compute, shell, browser, publish, media, voice, e-sign, delegation
— is withheld in environment suites until it has a simulator.

Files live under the hidden `/.eval/` tree (`inference.filesystem`), one
attempt folder per result: `/.eval/s<suite_id>/v<version>/attempts/<result>/`.
Fixtures come from the `EvalWorld` row itself, so preparing an attempt is
wipe + write with no pristine copy to drift: every attempt starts from the
same JSON and the JSON is what the reviewer accepted. Per-case
`__workspace__` overrides (`eval/workspace.py`) are applied on top, so a
case may add files without replacing the world.

World fixtures are **text** in v1 (`{path: text}`). Binaries the agent
produces during the run (a workbook, a deck) snapshot back normally for the
office graders; binary *fixtures* are a later phase.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

#: Worlds are capped per surface: big enough to be realistic, small enough
#: that generation stays cheap and every run re-reads them. From the plan §4.
MAX_WORLD_FILES = 30
MAX_WORLD_MAILS = 60
MAX_WORLD_EVENTS = 60
MAX_WORLD_KB_DOCS = 20
MAX_WORLD_PAGES = 25
#: One fixture file may not exceed this, so a world cannot smuggle a novel
#: past the reviewer or blow up every attempt's context.
MAX_FIXTURE_FILE_CHARS = 50_000

#: Surfaces, in build order. Later phases add their simulator + graders; the
#: files surface needs neither (real vfs rows) and the KB surface needs only
#: confinement (real hidden KB rows).
SURFACE_FILES = 'files'
SURFACE_KB = 'kb'
SURFACE_MAIL = 'mail'
SURFACE_CALENDAR = 'calendar'
SURFACE_DRIVE = 'drive'
SURFACE_WEB = 'web'
SURFACES = (SURFACE_FILES, SURFACE_KB, SURFACE_MAIL,
            SURFACE_CALENDAR, SURFACE_DRIVE, SURFACE_WEB)

#: Fixture files the world may hold, by extension. Text in, text out: a judge
#: emits JSON, so a fixture is always characters. Anything else is refused at
#: validation with the reason, rather than stored as a corrupt download.
TEXT_FIXTURE_EXTENSIONS = frozenset({
    'md', 'markdown', 'txt', 'text', 'log', 'json', 'csv', 'html', 'htm',
})


def live_world(suite):
    """The suite's live world: its newest `accepted` row, or None.

    None means the suite behaves exactly as today — no environment, no
    confinement, no withholding. Sync ORM; the runner wraps it.
    """
    from .models import EvalWorld

    return (EvalWorld.objects.filter(suite=suite, status='accepted')
            .order_by('-version').first())


def case_world_version(suite) -> int | None:
    """The world version a newly saved case belongs to, if any."""
    world = live_world(suite)
    return world.version if world is not None else None


def _norm(text: str) -> str:
    """Normalise for fact coverage: case and runs of whitespace must not
    decide whether two wordings state the same fact."""
    return ' '.join(str(text or '').lower().split())


def fact_covered(value: Any, haystack: str) -> bool:
    """Whether a planted fact value appears in fixture text.

    Two shapes: prose (substring after normalisation) and numbers/dates
    ("412,300" vs "412300", "2026-09-24" vs "24 Sep 2026" only when the
    digits run identically). A value that matches neither way is a fact the
    fixtures do not contain.
    """
    needle = _norm(value)
    if needle and needle in _norm(haystack):
        return True
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) >= 3:
        return digits in re.sub(r'\D', '', str(haystack or ''))
    return False


def validate_world(surfaces: dict, fixtures: dict, facts: list) -> list[str]:
    """Structural checks on a generated world. Returns error strings, empty
    when the world is well-formed. Pure — no rows, no provider.

    This is step 4 of the generation pipeline minus fact coverage (which
    needs the fixtures serialised per surface and lives in `step_check_world`
    in `eval/generator.py`): caps hold, paths are safe, extensions are text.
    """
    errors: list[str] = []
    surfaces = surfaces or {}
    fixtures = fixtures or {}

    files = (fixtures.get('files') or {})
    if not isinstance(files, dict):
        errors.append('files fixtures must be an object of path -> text')
        files = {}
    if len(files) > MAX_WORLD_FILES:
        errors.append(f'too many files ({len(files)} > {MAX_WORLD_FILES})')
    for path, content in files.items():
        segs = str(path or '').split('/')
        if not str(path or '').strip() or '..' in segs or str(path).startswith('/'):
            errors.append(f'unsafe file path {path!r}: use relative paths without ".."')
        ext = str(path).rsplit('.', 1)[-1].lower() if '.' in str(path) else ''
        if ext and ext not in TEXT_FIXTURE_EXTENSIONS:
            errors.append(
                f'{path!r}: binary fixtures are not supported in v1 '
                f'(only {sorted(TEXT_FIXTURE_EXTENSIONS)})')
        if len(str(content or '')) > MAX_FIXTURE_FILE_CHARS:
            errors.append(f'{path!r}: over the per-file cap')
        if not isinstance(content, str):
            errors.append(f'{path!r}: fixture content must be text')

    for surface, cap, key in (
            (SURFACE_MAIL, MAX_WORLD_MAILS, 'messages'),
            (SURFACE_KB, MAX_WORLD_KB_DOCS, 'documents'),
            (SURFACE_WEB, MAX_WORLD_PAGES, 'pages'),
            (SURFACE_CALENDAR, MAX_WORLD_EVENTS, 'events')):
        if surfaces.get(surface) is not True:
            continue
        items = (fixtures.get(surface) or {}).get(key) or []
        if not isinstance(items, list):
            errors.append(f'{surface} fixtures must hold a {key!r} list')
        elif len(items) > cap:
            errors.append(f'too many {surface} {key} ({len(items)} > {cap})')

    # A judge typo in a fixture id fails mid-run with "unknown message" —
    # name the missing field here instead, while the reply is still in hand.
    from .sim.calendar import REQUIRED_EVENT_KEYS
    from .sim.drive import REQUIRED_FILE_KEYS
    from .sim.mail import REQUIRED_MESSAGE_KEYS
    from .sim.web import REQUIRED_PAGE_KEYS

    for i, message in enumerate(
            ((fixtures.get(SURFACE_MAIL) or {}).get('messages') or [])):
        missing = [k for k in REQUIRED_MESSAGE_KEYS
                   if not (isinstance(message, dict) and message.get(k))]
        if missing or not isinstance(message, dict):
            errors.append(f'mail message {i}: missing {missing or "an object"}')
    for i, event in enumerate(
            ((fixtures.get(SURFACE_CALENDAR) or {}).get('events') or [])):
        missing = [k for k in REQUIRED_EVENT_KEYS
                   if not (isinstance(event, dict) and event.get(k))]
        if missing or not isinstance(event, dict):
            errors.append(f'calendar event {i}: missing {missing or "an object"}')
    for i, doc in enumerate(
            ((fixtures.get(SURFACE_KB) or {}).get('documents') or [])):
        if not (isinstance(doc, dict) and str(doc.get('name') or '').strip()):
            errors.append(f'kb document {i}: missing a name')
    if surfaces.get(SURFACE_DRIVE) is True:
        drive_files = (fixtures.get(SURFACE_DRIVE) or {}).get('files') or []
        if not isinstance(drive_files, list):
            errors.append('drive fixtures must hold a files list')
            drive_files = []
        for i, found in enumerate(drive_files):
            if not isinstance(found, dict):
                errors.append(f'drive file {i}: missing an object')
                continue
            # `content` may be legitimately empty; the id and name may not.
            missing = [k for k in REQUIRED_FILE_KEYS
                       if (k not in found) or (
                           k != 'content' and not str(found.get(k) or '').strip())]
            if missing:
                errors.append(f'drive file {i}: missing {missing}')
        sheet_ids = set()
        sheets = (fixtures.get(SURFACE_DRIVE) or {}).get('sheets') or {}
        if sheets and not isinstance(sheets, dict):
            errors.append('drive sheets must be an object of id -> tabs')
        else:
            sheet_ids = set(sheets or {})
        file_ids = {f.get('file_id') for f in drive_files if isinstance(f, dict)}
        for sid in sorted(sheet_ids):
            if sid not in file_ids:
                errors.append(f'sheet {sid!r} names no drive file')
    if surfaces.get(SURFACE_WEB) is True:
        pages = (fixtures.get(SURFACE_WEB) or {}).get('pages') or []
        if not isinstance(pages, list):
            errors.append('web fixtures must hold a pages list')
            pages = []
        for i, page in enumerate(pages):
            missing = [k for k in REQUIRED_PAGE_KEYS
                       if not (isinstance(page, dict) and page.get(k))]
            if missing or not isinstance(page, dict):
                errors.append(f'web page {i}: missing {missing or "an object"}')
        known_urls = {str(p.get('url')).rstrip('/') for p in pages
                      if isinstance(p, dict)}
        results = (fixtures.get(SURFACE_WEB) or {}).get('results') or {}
        if results and not isinstance(results, dict):
            errors.append('web results must be an object of query -> urls')
        else:
            for query, urls in (results or {}).items():
                for url in urls or []:
                    if str(url).rstrip('/') not in known_urls:
                        errors.append(
                            f'result for {query!r} names no page: {url!r}')

    if not isinstance(facts, list) or not facts:
        errors.append('a world needs at least one planted fact')
    else:
        for fact in facts:
            if not isinstance(fact, dict) or not fact.get('key') or 'value' not in fact:
                errors.append(f'a fact needs a key and a value, got {fact!r}')

    unknown = [s for s in surfaces if s not in SURFACES]
    if unknown:
        errors.append(f'unknown surfaces: {unknown}')
    return errors


@dataclass
class EvalEnvironment:
    """One attempt's world. Built per case run, never shared between attempts.

    `world` is the accepted `EvalWorld` row (read-only — never written here).
    Everything mutable — the attempt folder rows, the simulator states, the
    recorded calls — belongs to this object and dies with the attempt.
    """

    user: Any
    agent: Any
    suite: Any
    world: Any
    #: Simulators per surface, built in `prepare` from the fixtures.
    sims: dict[str, Any] = field(default_factory=dict)
    #: Every simulated call, in order, for the reviewer.
    calls: list[dict[str, Any]] = field(default_factory=list)
    #: `{path: text}` written by `prepare`, for the what-changed panel.
    pristine: dict[str, str] = field(default_factory=dict)
    #: Attempt folder row, set by `prepare`.
    attempt_folder: Any = None
    #: Hidden KB info `{'id', 'name', 'backend', 'doc_count'}`, set by
    #: `prepare` when the world has the KB surface (E-2).
    kb: dict[str, Any] | None = None
    #: `{document name: id}` for the hidden KB, set by `prepare` alongside
    #: `kb`. What the `cited` grader matches `read_document` calls against.
    kb_docs: dict[str, int] = field(default_factory=dict)

    # -- attempt lifecycle (ORM; the runner wraps these in sync_to_async) ----

    def prepare(self, case, spec: dict | None, attempt_key: str) -> str:
        """Reset every surface for one attempt. Returns the `{workspace}` path.

        Wipes the attempt folder (or creates it), writes the world fixtures,
        applies the case's `__workspace__` overrides on top, and resets the
        simulators. The agent's scope is rooted here, so `/` is the path its
        file tools accept — which is also what `{workspace}` in the goal
        becomes.
        """
        from inference import filesystem as fs
        from inference import vfs

        root = fs.ensure_folder(self.user, fs.EVAL_ROOT_NAME, None)
        parent = root
        for seg in (f's{self.suite.id}', f'v{self.world.version}',
                    'attempts', str(attempt_key)):
            parent = fs.ensure_folder(self.user, seg, parent)
        self.attempt_folder = parent
        _wipe_tree(self.user, parent)

        scope = self.attempt_scope()
        pristine: dict[str, str] = {}
        for rel_path, content in ((self.world.fixtures or {}).get('files') or {}).items():
            vfs.write_file(scope, '/' + str(rel_path).strip('/'), str(content or ''))
            pristine[str(rel_path).strip('/')] = str(content or '')
        for rel_path, content in ((spec or {}).get('files') or {}).items():
            vfs.write_file(scope, '/' + str(rel_path).strip('/'), str(content or ''))
        self.pristine = pristine

        self.sims = self._build_sims()
        self.calls = []
        self.kb = self._ensure_kb()
        self.kb_docs = self._kb_doc_ids()
        return '/'

    def attempt_scope(self):
        """The file scope the run gets: rooted at the attempt folder, fully
        writable inside it, blind to everything else — whatever the agent's
        own `fileAccess` says. Confinement comes from the walk, as with a
        `scoped` agent: every path resolves hop-by-hop from this root."""
        from inference.vfs import FileScope

        return FileScope(
            user=self.user, root=self.attempt_folder, mode='scoped',
            label='/', write_prefix=(), write_label='/',
        )

    def kb_scope(self) -> tuple[int, ...] | None:
        """The only KB the run may touch, or None when the world has none.

        None here does NOT mean "unrestricted the way an empty selection
        does" — when there is no KB surface the rag tools are withheld
        outright (see `withheld_names`), so this scope is only ever read
        alongside a hidden KB that exists.
        """
        return (self.kb['id'],) if self.kb else None

    def filter_gathered(self, gathered: dict) -> dict:
        """Swap the prompt's KB list for the world's hidden one.

        `_gather_context` prints the agent's configured KBs into the system
        prompt; in an environment run those rows are unreachable, so naming
        them would send the agent to ask for ids it will be refused. The
        hidden KB entry has the same `id/name/backend/doc_count` shape the
        prompt already renders.
        """
        if self.kb:
            gathered = dict(gathered)
            gathered['knowledge_bases'] = [dict(self.kb)]
        return gathered

    def snapshot_files(self) -> tuple[dict[str, str], dict[str, bytes]]:
        """`({path: text}, {path: bytes})` for the attempt folder afterwards.

        The same flat shape `GradeContext` takes from `workspace.snapshot` —
        graders cannot tell a world file from a workspace one, which is the
        point: the checks are identical, only the starting point is generated.
        """
        from inference import filesystem as fs
        from inference.models import Document
        from inference.vfs import is_binary
        from workflow_backend.thresholds import AGENT_FILE_BINARY_BYTES

        #: How much of one file a snapshot keeps — the same cap
        #: `eval/workspace.py` snapshots to, so the graders read the same map.
        SNAPSHOT_CHARS = 200_000
        files: dict[str, str] = {}
        binaries: dict[str, bytes] = {}
        if self.attempt_folder is None:
            return files, binaries
        base = fs.name_path(self.attempt_folder)
        docs = (Document.objects
                .filter(user=self.user,
                        folder__in=fs.subtree(self.attempt_folder, include_trashed=False))
                .select_related('folder'))
        for doc in docs:
            folder_path = fs.name_path(doc.folder)
            sub = folder_path[len(base):].strip('/')
            key = f'{sub}/{doc.name}' if sub else doc.name
            if is_binary(doc):
                try:
                    with doc.file.open('rb') as handle:
                        binaries[key] = handle.read(AGENT_FILE_BINARY_BYTES)
                except Exception:  # noqa: BLE001 - a missing file is not a failed grade
                    continue
            else:
                files[key] = (doc.content_text or '')[:SNAPSHOT_CHARS]
        return files, binaries

    def snapshot_env(self) -> dict[str, Any]:
        """Per-surface state for `GradeContext.env`, for the state graders."""
        out: dict[str, Any] = {}
        for surface, sim in self.sims.items():
            try:
                out[surface] = sim.snapshot()
            except Exception:  # noqa: BLE001 - a broken sim must not fail grading
                out[surface] = {}
        if self.kb:
            out['kb_id'] = self.kb['id']
            out['kb_docs'] = dict(self.kb_docs)
        return out

    def changes(self, files_after: dict[str, str]) -> dict[str, Any]:
        """What the attempt changed, for the `env_changes` panel.

        Files are diffed against what `prepare` wrote; every simulator
        reports its own mutations. Capped per list — this is a summary for a
        reviewer, not a second transcript.
        """
        written = sorted(
            p for p, text in files_after.items()
            if self.pristine.get(p) != text)[:50]
        removed = sorted(set(self.pristine) - set(files_after))[:50]
        out: dict[str, Any] = {'files_written': written}
        if removed:
            out['files_removed'] = removed
        for surface, sim in self.sims.items():
            try:
                delta = sim.changes()
            except Exception:  # noqa: BLE001
                continue
            if delta:
                out[surface] = delta
        if self.calls:
            out['simulated_calls'] = len(self.calls)
        return out

    # -- dispatch (pure; `run_simulated` is async for the toolbox) -----------

    def simulated_names(self) -> set[str]:
        """Tool names answered by simulators in this world."""
        names: set[str] = set()
        for sim in self.sims.values():
            names.update(getattr(sim, 'TOOLS', ()))
        return names

    def simulates(self, name: str) -> bool:
        """Whether this tool call goes to a simulator. Checked first in
        `AgentToolbox.dispatch`, before grants, scopes and credentials — a
        simulated Gmail send must never fall through to the real API."""
        return any(sim.handles(name) for sim in self.sims.values())

    async def run_simulated(self, name: str, args: dict, context: dict) -> str:
        """Answer one tool call from the matching simulator, recording it."""
        for sim in self.sims.values():
            if sim.handles(name):
                reply = sim.run(name, args or {})
                self.calls.append({
                    'tool': name,
                    'args': _redact(args or {}),
                    'summary': str(reply)[:500],
                })
                return reply
        return f"Error: '{name}' is not simulated in this evaluation world."

    def withheld_names(self, allowed: set[str]) -> set[str]:
        """Names to subtract from the toolbox in this world (fail closed).

        Kept: the infrastructure tools (plan, clock, archive), the file and
        office tools (real vfs rows, confined by the attempt scope), the
        sandbox (no network, files passed in), the KB readers when the world
        has a hidden KB — and whatever the simulators answer. Everything else
        has no fake version, so the agent does not get it at all: an offered
        tool that refuses on every call is worse than one never offered, and
        a tool that reaches a real service is an eval that sends real email.
        """
        from agents.agent.runtime import (
            ALWAYS_AVAILABLE, GRANT_TOOLS, RETRIEVAL_TOOLS,
        )

        keep = set(ALWAYS_AVAILABLE) | set(RETRIEVAL_TOOLS)
        for grant in ('fileOps', 'office', 'codeExecution'):
            keep.update(GRANT_TOOLS[grant])
        if self.kb is not None:
            # Real hidden-KB rows, confined by `kb_scope`: no simulator
            # needed, but only the readers. Anything that writes, extracts
            # or spends stays withheld — it has no confinement story yet.
            keep.update({
                'list_knowledge_bases', 'knowledge_base_search',
                'keyword_search', 'list_documents', 'read_document',
            })
        keep.update(self.simulated_names())
        return set(allowed) - keep

    # -- internals ------------------------------------------------------------

    def _build_sims(self) -> dict[str, Any]:
        """One simulator per world surface that has one. Files and KB are
        real rows, not simulations."""
        fixtures = self.world.fixtures or {}
        surfaces = self.world.surfaces or {}
        sims: dict[str, Any] = {}
        # Exactly `True`: `"pending"` is a grant with no builder yet, and a
        # simulator for it does not exist — attempting the import would fail
        # the attempt for a surface nobody promised.
        if surfaces.get(SURFACE_MAIL) is True:
            from .sim.mail import MailSim
            sims[SURFACE_MAIL] = MailSim(fixtures.get(SURFACE_MAIL) or {})
        if surfaces.get(SURFACE_CALENDAR) is True:
            from .sim.calendar import CalendarSim
            sims[SURFACE_CALENDAR] = CalendarSim(fixtures.get(SURFACE_CALENDAR) or {})
        if surfaces.get(SURFACE_DRIVE) is True:
            from .sim.drive import DriveSim
            sims[SURFACE_DRIVE] = DriveSim(fixtures.get(SURFACE_DRIVE) or {})
        if surfaces.get(SURFACE_WEB) is True:
            from .sim.web import WebSim
            sims[SURFACE_WEB] = WebSim(fixtures.get(SURFACE_WEB) or {})
        return sims

    def _ensure_kb(self) -> dict[str, Any] | None:
        """The world's hidden KB (E-2). None until the world has that surface.

        Exactly `True`: a `"pending"` surface is a grant with no builder yet,
        and building an empty KB for it would hand the run a corpus the
        reviewer never saw.
        """
        if (self.world.surfaces or {}).get(SURFACE_KB) is not True:
            return None
        from . import kb_world

        return kb_world.ensure_world_kb(
            self.user, self.suite, self.world, self.world.fixtures.get(SURFACE_KB) or {})

    def _kb_doc_ids(self) -> dict[str, int]:
        if not self.kb:
            return {}
        from . import kb_world

        try:
            return kb_world.doc_ids(self.user, self.kb['id'])
        except Exception:  # noqa: BLE001 - grading must not depend on it
            return {}


def for_attempt(user, agent, suite, case) -> EvalEnvironment | None:
    """The environment one case attempt runs in, or None for legacy suites.

    None when the suite has no accepted world: the run behaves exactly as
    today. Sync ORM; the runner wraps it.
    """
    world = live_world(suite)
    if world is None:
        return None
    return EvalEnvironment(user=user, agent=agent, suite=suite, world=world)


def _wipe_tree(user, folder) -> None:
    """Hard-delete a folder's contents but keep the folder itself.

    Attempt scratch, never the bin: like `workspace._wipe` but keeping the
    attempt row, so its id stays stable for the run.
    """
    from inference import filesystem as fs
    from inference.models import Document, Folder

    folders = list(fs.subtree(folder, include_trashed=False)
                   .exclude(pk=folder.pk).values_list('id', flat=True))
    Document._base_manager.filter(user=user, folder_id__in=folders).delete()
    Document._base_manager.filter(user=user, folder=folder).delete()
    for row in Folder.all_objects.filter(id__in=folders).order_by('-path'):
        row.delete()


def _delete_folder_tree(user, folder) -> int:
    """Hard-delete a hidden eval folder and all its descendant files/folders."""
    from inference import filesystem as fs
    from inference.models import Document, Folder

    folder_ids = list(fs.subtree(folder).values_list('id', flat=True))
    documents = Document._base_manager.filter(
        user=user, folder_id__in=folder_ids,
    )
    for document in documents.iterator():
        # FileField storage is not automatically purged by Django when its row
        # is deleted. Office outputs can be binary and much larger than their
        # result row, so remove their stored bytes too.
        if document.file:
            document.file.delete(save=False)
        document.delete()
    for row in Folder.all_objects.filter(id__in=folder_ids).order_by('-path'):
        row.delete()
    return len(folder_ids)


def delete_attempts(user, suite_id: int, world_version: int | None,
                    result_ids) -> int:
    """Hard-delete hidden file trees owned by the given eval results.

    World fixtures are copied into one `/.eval/.../attempts/<result>/` folder
    per attempt. They are retained while the result is reviewable, but become
    orphaned data when a result or its sweep is dismissed. Only the exact
    result folders under this user's suite/version are removed; the shared
    world fixtures and other attempts stay intact.
    """
    if world_version is None:
        return 0

    from inference import filesystem as fs

    parent = fs.eval_root(user)
    for name in (f's{suite_id}', f'v{world_version}', 'attempts'):
        if parent is None:
            return 0
        parent = fs.child_by_name(user, parent, name)
    if parent is None:
        return 0

    removed = 0
    for result_id in set(result_ids):
        attempt = fs.child_by_name(user, parent, str(result_id))
        if attempt is None:
            continue
        _delete_folder_tree(user, attempt)
        removed += 1
    return removed


def delete_suite_files(user, suite_id: int) -> bool:
    """Remove this suite's hidden eval tree, including orphaned old attempts."""
    from inference import filesystem as fs

    root = fs.eval_root(user)
    suite_folder = fs.child_by_name(user, root, f's{suite_id}') if root else None
    if suite_folder is None:
        return False
    _delete_folder_tree(user, suite_folder)
    return True


def _redact(args: dict) -> dict:
    """Simulated call args for the reviewer: full shape, no secret values."""
    redacted = {}
    for key, value in args.items():
        if any(token in key.lower() for token in ('token', 'secret', 'password', 'key')):
            redacted[key] = '…'
        else:
            redacted[key] = value if isinstance(value, (str, int, float, bool)) else '…'
    return redacted


__all__ = [
    'SURFACES', 'SURFACE_FILES', 'SURFACE_KB', 'SURFACE_MAIL',
    'SURFACE_CALENDAR', 'SURFACE_DRIVE', 'SURFACE_WEB',
    'MAX_WORLD_FILES', 'MAX_WORLD_MAILS', 'MAX_WORLD_EVENTS',
    'MAX_WORLD_KB_DOCS', 'MAX_WORLD_PAGES',
    'MAX_FIXTURE_FILE_CHARS', 'TEXT_FIXTURE_EXTENSIONS',
    'EvalEnvironment', 'fact_covered', 'for_attempt', 'live_world',
    'case_world_version', 'delete_attempts', 'delete_suite_files', 'validate_world',
]
