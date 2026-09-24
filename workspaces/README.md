# `workspaces/`: a private machine per user (not working yet)

The coding agents (the `code` pack) need a persistent Linux machine per user,
to read, edit and run a real repository. This app holds the records for it:

| Model | What it is |
|---|---|
| `Workspace` | One user's machine |
| `WorkspaceJob` | A long command running on it |
| `CodeProject` | A repository in the workspace |
| `CodeChange` | One file a worker changed |
| `CodeLease` | A lock on files, so two workers don't edit the same file |

> **Status:** `engine.py` is a stub. Every call raises, because no engine is
> built yet (`WORKSPACE_ENGINE=none`). While that is true, the coding tools are
> hidden from agents. See [`docs/COMPUTE_ISOLATION_PLAN.md`](../docs/COMPUTE_ISOLATION_PLAN.md).

## Files

| File | What it does |
|---|---|
| `engine.py` | The one door to a workspace (`ensure`, `exec`, `read`, `write`...). A stub today |
| `leases.py` | File locks for coding workers |
| `reads.py` | Remembers which version of each file a run read, so an edit to a file that changed since is refused |
| `awareness.py` | Tells other running workers when a file they care about changed |
| `sweep.py` | Pauses idle workspaces |
| `views.py`, `urls.py` | One webhook: a job finished |

Tests: `workspaces/tests/`.
