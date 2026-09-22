# A cloud computer per user: isolation plan

Status: **for discussion**, 2026-09-22. Nothing here is built yet.
Supersedes the provider question left open as decision D1 in
`PLATFORM_CAPABILITIES_PLAN.md` §8/§14, and answers two questions the owner
asked: *should we use Docker sandboxes or VMs?* and *should the machine be
per agent or per user?*

Written for: the owner (to decide) and the engineer who builds it (§8 onward
is step-by-step).

---

## 1. The short answer

1. **Yes to isolation, but a plain Docker container is not enough for this job.**
   Containers share the host's Linux kernel. That is fine for our snippet
   sandbox, which has no network, no persistence and a 30-second life. It is
   not fine for a machine where a model runs any shell command it likes, for
   hours, with the internet. Use **microVMs** (Firecracker) or
   **gVisor-sandboxed containers**. Both give each user their own kernel
   boundary, start in about a second, and cost far less than full VMs.
2. **Never on our 913 MB box.** Not one user machine runs on the server that
   hosts the website and database. They run on a provider (or, later, a
   separate host we rent). That is what actually stops "one process kills the
   entire system": the user's machine and our platform don't share RAM, CPU
   or kernel.
3. **One machine per user, not per agent.** Agents are separated *inside*
   the user's machine (own folder, own process limits). Anything we don't
   trust with the user's files, such as an agent installed from someone
   else's template, gets a **throwaway sandbox per run** instead. §4
   explains why.
4. **Most of the plumbing already exists.** `workspaces/` has the models, the
   one-door engine, the tools (`workspace_exec`, `start_job`, `ws_run`…), the
   grants and the idle sweep. **The engine behind it is a stub:** every method
   raises "no engine configured". The work is mostly one thing: implement
   that engine for one provider.

---

## 2. What exists today

| Piece | What it is | Isolation | State |
|---|---|---|---|
| `sandbox_service/` (sidecar) | Runs `execute_python` / `run_python_on_files` snippets | **One container shared by all users.** Each snippet is its own process + temp dir with `setrlimit` (CPU, memory, file size), `killpg` on timeout, best-effort seccomp. Container: no network (`internal: true`), `cap_drop: ALL`, read-only root, non-root, 300 MB, 128 pids, 2 concurrent runs | **Live in prod.** Good at what it does. Keep it. |
| `workspaces/models.py` | `Workspace` (**one per user**, `OneToOneField`), `WorkspaceJob`, `CodeProject`, `CodeChange`, `CodeLease` | n/a | Built, migrated |
| `workspaces/engine.py` | One door: `ensure / exec / read / write / listdir / hibernate / destroy`, `WORKSPACE_ENGINE = none \| docker \| <provider>` | n/a | **Stub. Every call raises.** No `docker` engine exists despite the docstring |
| `chat/tools/compute.py`, `code.py` | `workspace_exec`, `start_job`, `job_status`, `job_logs`, `cancel_job`, `sync_files`, `ws_*`, `git_*` | Only need `exec`, `read`, `write`, `listdir` from the engine | Built; hidden while engine is `none` |
| `workspaces/sweep.py` | Idle hibernate | n/a | Built (and needs a scheduler; see `SCHEDULES_SIMPLIFICATION_PLAN.md` B1) |

So the snippet sandbox is already safe for its job, and the "cloud computer"
has a body but no engine.

### Why the sidecar can't simply become the cloud computer

It is **shared** by all users: two users' snippets run side by side in one
container, separated only by processes and temp folders. That is acceptable
*only* because nothing persists, nothing reaches the network, and every run
dies in 30 s. The moment a user gets persistent files, `pip install`,
internet access or a 6-hour job, those processes live next to other users'
processes and files under one kernel. One `fork` loop or a container escape
would then affect everyone. So it stays the stateless snippet runner, and the
cloud computer is a separate thing.

---

## 3. Docker vs gVisor vs microVM: what "isolation" actually buys

| | Plain Docker container | Docker + **gVisor** (`runsc`) | **Firecracker microVM** | Full VM (EC2 per user) |
|---|---|---|---|---|
| Kernel | Shared with host | Shared, but syscalls go through a user-space kernel | **Own kernel** | Own kernel |
| Escape = | Host root (one kernel bug away) | Much harder; gVisor bug + host bug | Hypervisor bug (very rare) | Hypervisor bug |
| Start time | <1 s | ~1 s | ~1 s (snapshots: ms) | 30–60 s |
| Memory overhead | ~0 | small | ~5 MB + guest | 100s of MB |
| Good for | Our snippet sidecar | Self-hosting user machines on our own host | **User machines (hosted)** | Too slow and costly per user |
| Who offers it | us | Modal (uses gVisor); self-host on any Linux | E2B, Fly Machines (both Firecracker-based) | AWS |

**Recommendation:** microVMs from a provider for v1. We get kernel-level
separation per user without running a hypervisor fleet on a 913 MB server.
Use Docker + gVisor for **local development** and as the self-host option if
provider cost becomes a problem at scale.

Two Docker rules, if Docker is ever used for user code:
- **Never mount `/var/run/docker.sock` into the backend.** Anyone who can
  talk to that socket is root on the host. A backend that launches containers
  does it through a small separate "launcher" service with a narrow API, or
  through the provider's API.
- A container running user shell commands gets `--runtime=runsc` (gVisor),
  `--network` restricted to an egress proxy, `--memory`, `--pids-limit`,
  `--cpus`, `--read-only` root + a writable volume for `/home/user`, and no
  host mounts.

(Before choosing, re-check each provider's current isolation technology and
pricing. The table reflects what they published as of this writing, not a
measurement.)

---

## 4. Per user or per agent?

### Recommendation: **one machine per user**, agents separated inside it

| Question | Per-user machine | Per-agent machine |
|---|---|---|
| Matches "a cloud computer for each user"? | **Yes.** One computer, like a laptop | No. A user with 8 agents has 8 computers |
| Agents sharing files (researcher writes, writer reads) | Same disk, just folders | Needs copying between machines |
| Cost (machines are billed while awake) | 1 × users | agents × users; typically 3–10× more |
| Cold starts | One warm machine serves every agent | Each agent wakes its own |
| `pip install` once, use everywhere | Yes | Per agent, again and again |
| Blast radius of a bad agent | That user's machine (their own data) | One agent's machine |
| Fits existing code | **Yes.** `Workspace.user` is `OneToOneField`, the VFS, `FileScope` and `CodeProject` are all per user | Would need a model change and every tool re-scoped |

The per-agent column wins only on blast radius, and that radius is *the
user's own data*, which the user's own agent was already allowed to touch
through its grants. The threat per-agent machines guard against, "agent A
damages agent B's work", is handled more cheaply inside the machine:

**Separation inside the user's machine:**
- **Folders:** each agent works in `/home/user/agents/<agent-slug>/`, each
  code project in `/home/user/projects/<name>/`. The existing `FileScope`
  rule (`read_all_write_own`: read everything, write only your own folder)
  applies to the machine's disk the same way it applies to the VFS today.
  Code projects already use `CodeLease` so two runs can't write the same file.
- **Per-command limits:** every `exec` runs under `systemd-run --scope` (or
  `prlimit` + `timeout` if the image has no systemd) with `MemoryMax`,
  `CPUQuota`, `TasksMax` and a wall-clock timeout. So one runaway command
  can't starve the other agents or the machine's own agent process. That is
  "one process can't kill the system" at the inner level. The provider's VM
  boundary is the outer level.
- **Per-run process group:** a run's processes are killed with its run
  (cancel, timeout, finish) via the job's cgroup/scope, so nothing a
  finished run started keeps eating the user's quota.

### The exception: a throwaway sandbox per run

Some runs should **not** get the user's machine at all:
- An agent **installed from someone else's template** (Explore, `/a/<slug>`)
  until the user marks it trusted: it hasn't earned the user's files.
- Runs with the `compute` grant but **no `fileAccess` scope**.
- Anything scheduled + unattended + `autonomy: full` that only needs to
  compute. A fresh machine per run is the safest place for a run nobody watches.

These get an **ephemeral machine**: same engine, `ensure_ephemeral(run)`,
destroyed at run end, no user files mounted, inputs passed in explicitly.
Build this second, after the per-user machine works.

So the final shape is **three tiers**, cheapest first:

```
 snippet ─▶ sandbox_service sidecar   (exists; shared, stateless, no network, 30 s)
 agent   ─▶ the user's machine        (one per user; persistent; hibernates when idle)
 untrusted run ─▶ ephemeral machine    (one per run; destroyed after)
```

---

## 5. Rules every tier keeps (already written in `workspaces/models.py`, restated)

1. **No platform secrets inside a machine**: no `.env`, no DB URL, no
   `SECRET_KEY`. User secrets enter only as `credentials/refs.py`
   references resolved into **one command's** environment after approval,
   and are scrubbed from output.
2. **Egress through an allowlist**: default PyPI, npm, GitHub, plus the
   agent's `workspaceEgress`; always blocked: `169.254.169.254` (cloud
   metadata), our own backend's private address, private ranges.
3. **Idle hibernate** after 15 min (`WORKSPACE_IDLE_SECONDS`); disk survives,
   RAM doesn't.
4. **Quotas per tier** (decision D8): machine size, disk GB, CPU-minutes per
   day, concurrent jobs. Metered into `CostEntry` (kind `compute`) so the spend
   cap covers it.
5. **Destroyed with the account.**
6. **The machine can't call our backend** except the job-finished webhook
   (`/api/workspaces/hooks/<secret>/`), which answers 404 for every refusal like
   every other secret-in-path route.

---

## 6. Decisions for the owner (please answer these)

| # | Question | Options | My recommendation |
|---|---|---|---|
| C1 | Provider for v1 | E2B · Fly Machines · Modal · Daytona · self-host (gVisor on a rented box) | Spike **E2B** and **Fly Machines** (§8 Phase A) and pick on the table in §8. Both are Firecracker-based. |
| C2 | Who gets a machine | Everyone · paid tiers only · invite-only first | **Invite-only first** (owner + a few testers), because machines cost money while awake |
| C3 | Machine size v1 | 1 vCPU/1 GB · 2 vCPU/2 GB · 2 vCPU/4 GB | **1 vCPU / 2 GB**, disk 5 GB (the model's current default) |
| C4 | Keep disk when hibernated? | Yes (pay storage) · No (fresh each time) | **Yes**. Otherwise it's not "a computer", just a sandbox |
| C5 | Region | India (Mumbai) required? | Prefer a region near `ap-south-1`; latency matters for the terminal, less for agents |
| C6 | Untrusted-agent ephemeral tier | In v1 · later | **Later** (Phase D). Until then, installed-from-others agents simply aren't offered the `compute`/`shell` grants |

---

## 7. What users will see (keep it simple)

- **One page: "Your computer"** (`/computer`): status (Asleep / Waking /
  Running), disk used, CPU minutes left today, **Wake** / **Sleep** / **Reset**
  buttons, and a file list of `/home/user`. Later a terminal (xterm.js, already
  planned for the Code tab).
- Agents that use it show a small "uses your computer" chip in the builder;
  the `compute`/`shell` grants say "Runs commands on your cloud computer".
- When it's asleep, the first command wakes it and the chat shows "Waking
  your computer…" (a status event), not silence.
- **Reset** wipes and recreates it (with a confirm), the escape hatch for
  "I broke my machine".

---

## 8. Build plan (after the owner answers §6)

### Phase A: provider spike (1–2 days, no production code)

For each shortlisted provider, write a throwaway script in
`Backend/scripts/spike_<provider>.py` that:
1. creates a machine from a Debian/Ubuntu image with Python 3.12 + git,
2. runs `echo hi`, `pip install requests`, writes `/home/user/a.txt`,
3. hibernates / pauses it, waits 5 min, resumes, checks `a.txt` **and** the
   installed package survived,
4. runs a command that allocates 3 GB (must be killed, machine must survive),
5. tries `curl http://169.254.169.254/` (must fail once egress rules are set),
6. measures: create time, resume time, exec round-trip from our EC2 region.

Fill in:

| | Provider 1 | Provider 2 |
|---|---|---|
| Disk survives pause? | | |
| Resume time | | |
| Exec round-trip from ap-south-1 | | |
| Egress allowlist supported how? | | |
| PTY / port forward (for the Code tab) | | |
| Idle cost per user per month (paused) | | |
| Active cost per CPU-hour | | |
| Python SDK async? | | |

Pick the winner and record it in §10.

### Phase B: the engine (the real work)

1. `workspaces/engines/` package: `base.py` (an abstract `Engine` with the
   seven methods from `engine.py`), `<provider>.py`, `docker_gvisor.py`.
2. `workspaces/engine.py` keeps its public functions and dispatches to the
   class chosen by `WORKSPACE_ENGINE`. **No tool changes.** `compute.py` and
   `code.py` already call `engine.exec/read/write/listdir`.
3. `ensure(user)`: create or resume; set `provider_id`, `status`,
   `last_active_at`. Must be safe to call concurrently. Use
   `select_for_update` on the `Workspace` row so two runs don't create two
   machines.
4. `exec`: wraps the command in the per-command limits from §4 (`systemd-run
   --scope -p MemoryMax=… -p CPUQuota=… -p TasksMax=…` + `timeout`), returns
   `{exit_code, stdout, stderr}` capped at the existing output limits.
5. The provider SDK is an **optional, import-guarded** dependency (as
   `PLATFORM_CAPABILITIES_PLAN.md` §13.3 requires): if it's missing,
   `workspace_available()` is False and the tools are hidden.
6. `docker_gvisor.py` is for dev only: it talks to a local Docker daemon **from
   the dev machine**, refuses to start unless `runsc` is available (or
   `WORKSPACE_ALLOW_RUNC_DEV=True` is set explicitly), and is never enabled in
   `settings/deployment.py`.
7. Egress: provider firewall rules if supported, otherwise an egress proxy
   (the P0 `check_egress` allowlist) set as `HTTP(S)_PROXY` in the machine.
8. Settings: `WORKSPACE_ENGINE`, `WORKSPACE_PROVIDER_API_KEY` (platform
   secret, lives only in the backend's `.env`), `WORKSPACE_IDLE_SECONDS`,
   `WORKSPACE_DEFAULT_CPU`, `WORKSPACE_DEFAULT_MEM_MB`, `WORKSPACE_DISK_GB`.
   Add them to `CLAUDE.md` Environment Variables.

**Tests** (`workspaces/tests/test_engine_contract.py`): one contract suite
run against a fake engine in CI and against the real provider in a nightly
job marked `@pytest.mark.provider`: create → exec → write → read → listdir →
hibernate → resume → file still there → destroy. Isolation tests: user A's
`ensure` never returns user B's machine; `env` inside the machine contains no
`SECRET_KEY`/`DATABASE_URL`/`CREDENTIAL_ENCRYPTION_KEY`; the metadata IP is
unreachable; a 3 GB allocation is killed and the next `exec` still works.

### Phase C: quotas, metering, the page

1. `CostEntry(kind='compute')` per `exec`/job from measured wall-clock × size.
2. Quota check in `ensure`/`exec`/`start_job`, with a reason the model and
   the user can read ("Daily compute used up. Resets at 00:00 IST.").
3. `/computer` page (§7) + `GET/POST /api/workspaces/me/` (status, wake,
   sleep, reset). Update `Backend/docs/API.md`.
4. Hibernate sweep runs on the in-process scheduler from
   `SCHEDULES_SIMPLIFICATION_PLAN.md` Phase 5, not on a crontab.

### Phase D: ephemeral per-run machines (optional, after C)

`engine.ensure_ephemeral(execution)` + `destroy` in the run's `finally`,
used when §4's exception rules match. Nothing is mounted from the user; files
go in through `sync_files`-style explicit inputs.

---

## 9. Not doing

- A machine per agent (§4).
- User code on the 913 MB production host, in any form.
- Mounting the Docker socket into the backend.
- GPUs in v1.
- BrowserOS "Files app" over the machine (BrowserOS is parked).
- Replacing the snippet sidecar: it stays for `execute_python`.

## 10. Decisions log (fill in)

- C1 provider: ___ (spike results: ___)
- C2 who gets one: ___
- C3 size: ___
- C4 keep disk: ___
- C5 region: ___
- C6 ephemeral tier: ___
