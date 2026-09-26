# VFS hardening plan (2026-09-26)

Scope: `inference/vfs.py` and the file tools in `chat/tools/files.py`.
Status: implemented 2026-09-26 (see "Verification" for what was and was not run).

## Ground rules

1. **No existing behaviour changes for a caller that passes no new argument.**
   Every new parameter is optional; every tool keeps its name and result keys.
2. **No schema change to `Document`.** A unique constraint was considered and
   rejected: uploads (`inference/views.py`), chat attachments
   (`chat/sources/attachments.py`), Imagine outputs, the eval KB world and
   `filesystem.move` all create or move documents with no name check, so
   same-named siblings already exist and are created by design. A constraint
   would turn a second upload of `report.pdf` into a 500, and its migration
   would fail on existing data. The fix is confined to the VFS write path.
3. **Nothing new may widen a scope.** Every new write verb calls
   `_require_write_at` on *both* ends, before touching a row.
4. **A migration must never block a deploy.** The only migration is a
   Postgres-only index created inside a savepoint; any failure (no
   `pg_trgm`, no privilege, SQLite) is logged and skipped.

## 1. Duplicate names from concurrent writes

*Problem.* `write_file`/`write_binary` do look-up-then-create with no lock, so
two workers writing one new path both create a row; `_document_in` then picks
one arbitrarily (`ordering = ['-created_at']` + `.first()`).

*Fix.*
- `_name_lock(user, folder, name)`: a context manager that opens
  `transaction.atomic()` and, on Postgres, takes
  `pg_advisory_xact_lock(<64-bit hash of user|folder|name>)`. On SQLite the
  project already runs `transaction_mode=IMMEDIATE`, so `BEGIN` takes the
  database write lock and serialises the section by itself.
- `write_file`, `write_binary`, `edit_file`, `move` and `copy` do
  look-up → decide → create/update inside that lock. `write_binary` writes its
  bytes to storage inside the lock (as before, deleting them if the row fails):
  the name the bytes are filed under is only known once the lock has decided
  it, and storage here is the local media volume, so the hold is short.
- Residual, stated: `write_binary`'s `name (2).ext` fallback is decided under
  the lock of the *requested* name, so two renders both falling back at the
  same instant could still pick the same numbered name.
- `_document_in` becomes deterministic: `order_by('id')` — the oldest row is
  the file; duplicates that already exist resolve the same way every time for
  read, edit, write and delete.

*Refused alternatives.* A DB constraint (rule 2); `select_for_update` on the
parent folder (the root is `NULL`, there is no row to lock).

## 2. Lost updates between writers

*Problem.* Read → overwrite by two workers silently discards one change.

*Fix.* `read_file`, `write_file`, `edit_file` return `version` (the document's
`updated_at`, ISO). `write_file` and `edit_file` accept optional
`expected_version`; when given and stale (`office_edit.is_stale`, the same
check the apps' autosave uses, including its draft-render exception) the write
is refused with a message telling the model to re-read. The check runs inside
the name lock so check-and-write is atomic. `expected_version` on a file that
does not exist is refused (someone deleted or renamed it). Absent = today's
behaviour.

## 3. `find_files`: speed and usefulness

- Each match gains `snippets`: up to 3 `{line, text}` hits (line ≤ 200 chars)
  computed in Python from the row already fetched — no extra query.
- Migration `0024_document_trigram_search`: on Postgres only,
  `CREATE EXTENSION IF NOT EXISTS pg_trgm` and GIN indexes on
  `UPPER(name)` and `UPPER(content_text)` with `gin_trgm_ops` — `UPPER(...)`
  because that is what Django emits for `icontains` on Postgres. Savepoint +
  skip on failure (rule 4). Reverse drops the indexes.
- The docstring stops claiming an index exists on databases where it doesn't.

## 4. Move / rename / copy

New `vfs.move(scope, src, dst)` and `vfs.copy(scope, src, dst)`, tools
`move_file` and `copy_file` (`fileOps` grant, `sensitive`, `effect="reversible"`).

- `dst` naming an existing directory → into it, keeping the name; otherwise
  `dst` is the new full path (parent dirs created, `mkdir -p` as `write_file`).
- Write permission required at `src`'s parent **and** at `dst` (move); at
  `dst` only (copy — reading `src` is confined by the walk).
- Refused: a taken destination name (no silent overwrite, no silent
  renumbering); moving a folder into itself/descendant (`filesystem.move`
  already refuses); moving the scope root or a writable root itself
  (`/Agents/<name>`, `/Chat`); changing a binary file's extension.
- Documents: rename + reparent in one `update()` under the name lock of the
  destination. Folders: `filesystem.move` then `rename_folder`, in one
  transaction. Neither touches `knowledge_base`/indexing — a move is a column
  write, per `filesystem.py`'s rule. Id and version history are preserved.
- Copy is file-only (`office_edit.copy`, then renamed to the target under the
  lock). Copying a directory tree is out of scope — refused with a message.

## 5. Line-addressed reads

`read_file` accepts optional `start_line` / `end_line` (1-based, inclusive).
When given, the result is those lines prefixed `N: `, still capped by the
character window, with `total_lines` and a continuation note. Character
`offset` mode is unchanged and remains the default.

## 6. Case-insensitive folder resolution

- `_folder_at` (reads): exact match first; on a miss, if exactly one sibling
  matches case-insensitively, the error names it ("did you mean /Reports/?").
  Reads do not silently follow a different case.
- `_make_dirs` (writes): exact match first; else reuse a *single*
  case-insensitive match instead of creating `reports/` beside `Reports/`.
  Two or more candidates → exact behaviour (create), never a guess.
- File names stay exact; `No such file` gains the same "did you mean" hint.

## 7. Recursive listing

`list_dir(depth=1..3)` / `list_files(depth)`. `depth=1` is today's output
exactly. Deeper returns an additional `tree` of nested entries, with a total
entry budget equal to the listing limit and `truncated` when hit.

## Wiring checklist

- `agents/grants.py` `fileOps`, `tools_config/views.py` library list,
  `chat/tools/describe.py` phrases, `chat/tests/test_rework.py::FILE_TOOLS`.
- Chat orchestrator scope: new tools are `reversible`, so chat withholds them
  automatically (`chat_orchestrator_allowed`).
- CLAUDE.md: one paragraph under the VFS notes.

## Tests (`inference/tests/test_vfs_hardening.py`)

One class per item: deterministic duplicate resolution + write targets the
oldest; lock is taken on every write path; stale `expected_version` refused,
fresh accepted, absent unchanged; snippets with line numbers; move/rename/copy
happy paths + every refusal above + scope checks for read-only and
`read_all_write_own`; line reads; case fallback for reads (hint) and writes
(reuse, ambiguity); depth listing + budget. Plus the existing `test_vfs*`,
`test_rework`, `test_orchestrator_scope`, `test_autonomy`,
`tools_config` suites must stay green.
