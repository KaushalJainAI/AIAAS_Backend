# Platform Capabilities Plan — messaging, data, browser, compute, Code tab, long runs, dashboards, auto mode

Status: **P0–P10 implemented 2026-09-21 in this working tree
(P0 and P3 committed, the rest uncommitted; the `/code`, `/missions` and
`/dashboards` pages are not built).** Written
to be handed to another engineer or AI to implement phase by phase. Each phase
lists the gap, the design, the files, the tests and the exit criteria. Read §1
and §2 before touching any phase: they are the rules every phase follows, and
most of them are already enforced by code and tests.

Scope, as the owner decided it:

- Build **four of the five** capability packs proposed in the review
  (messaging, data, browser actions, bigger compute, voice + documents).
  **Payments are out**: no payment links, no Razorpay/Stripe/UPI.
- Build a **Code tab**: a per-user, sandboxed coding workspace that we host
  ourselves (an OpenCode/Claude Code-style agent with a terminal, file tree and
  diffs). It is driven through our own scoped tools and **does not embed
  OpenCode**. It is remote-controlled from the web app, so it keeps running
  when the tab is closed.
- **Long-horizon tasks**: work that spans hours or days and many runs.
- **Better dashboards**: for the owner watching agents, *and* dashboards an agent
  builds for the user.
- **Auto mode**: something a user can switch on in chat and on agents, and that
  is safe enough that people actually use it.

Only the Backend and `better-n8n-frontend/` are in scope. **BrowserOS is parked
and is not touched** (see CLAUDE.md).

---

## 0. Phase map

| # | Phase | Size | Depends on | Unlocks | State (2026-09-21) |
|---|-------|------|------------|---------|--------------------|
| P0 | Foundations: secret references, cost ledger, egress guard, capability registry | M | — | everything | **Done, committed.** `credentials/refs.py`, `logs/costs.py` + `CostEntry`, `check_egress`, `GET /api/orchestrator/capabilities/`. |
| P1 | `talk`: Slack, WhatsApp, Teams, SMS | L | P0 | briefs, follow-ups, reminders | **Done in tree, uncommitted.** `messaging/` app (`MessagingAccount`, `OutboundMessage`, `InboundMessage`, webhooks, retention sweep, beat task + `purge_inbound` command); `chat/tools/talk.py` + `chat/tools/messaging/{common,slack,whatsapp,teams,sms}.py` (`message_channels/search/read/draft/send`, 20/run + 5/recipient caps, unattended `recipients` gate, ledger cost for WhatsApp/SMS); 16 tests (`messaging/tests/test_messaging.py`). |
| P2 | Browser Pro: sessions, vault logins, downloads, live view | M | P0 | portal work (GST, courier, bank, vendors) | **Done in tree, uncommitted.** `browsing/models.py::BrowserSession` (+migration, sweep command + beat task), session-scoped `browser_act` with `session`/`trace`, `fill_secret` via P0 refs against `browserLogins`, `ask_user` OTP/CAPTCHA pause, downloads → VFS, submit gate feeding P3 reviewer, per-minute ledger; new step verbs (`scroll`, `download`, `extract`); tests (`browsing/tests/test_sessions.py`). Uploads still refused by design. |
| P3 | Auto mode: chat autonomy, account default, action reviewer | M | — | using the platform without clicking all day | **Done, committed** (P0/P3 commit `94a9292` + docs `2b7d907`; 24 backend + 2 frontend tests). |
| P4 | `data`: SQL + generic API caller | L | P0 | CRM/DB work without a connector per system | **Done in tree, uncommitted.** `data/` app (`DataConnection`, `ApiConnection`, one migration); `data/drivers.py` + `data/sqlcheck.py` (parsed reads, read-only txn, 30 s timeout, 1,000 inline rows → CSV spill, max 50k spill rows); `chat/tools/data.py` (`list_data_connections`, `describe_schema`, `query_sql`, `execute_sql`) + `chat/tools/apicaller.py` (`list_api_operations`, `call_api`); `dataConnections`/`apiConnections`/`dbHosts`/`apiHosts` scopes, both doors; 18 tests (`data/tests/test_data.py`). |
| P5 | Compute plane + `run_code` Pro (pip, internet, long jobs) | L | P0 | real pipelines, scheduled jobs | **Done in tree, uncommitted.** `workspaces/` app (models + `0001_initial` + `engine.py` one-door `none/docker/<provider>`, `ensure/exec/read/write/listdir/hibernate/destroy`, views + `hooks/<secret>/` → `job.finished` event, sweep + `sweep_workspaces` command + beat task, `WORKSPACE_ENGINE/WORKSPACE_IDLE_SECONDS` + quotas); `chat/tools/compute.py` (`workspace_exec`, `start_job`, `job_status`, `job_logs`, `cancel_job`, `sync_files`, `requires='workspace'`); `compute` grant end-to-end (runtime, TOOL_KEYS, builder help, capabilities scope map + engine gate, frontend labels, tools library). Tests: `chat/tests/test_phases_p5_p8.py`. |
| P6 | Code tab (serves the `shell` grant) | XL | P5, P3 | coding agent, sandboxed per user | **Done in tree, uncommitted (backend + tools; `/code` page not built).** `shell` served: `GRANT_TOOLS['shell']` (12 `ws_*`/`git_*` tools in `chat/tools/code.py`), `UNSERVED_GRANTS` empty, `codeProjects` scope both doors, `CodeProject` + `CodeChange` (+migration `0002`), tools library `shell` category real, builder `TOOL_HELP` + toggles + types. Frontend `/code` page (Monaco/xterm/diff review) not built. |
| P7 | Long-horizon missions | L | P3 (P5 optional) | multi-day goals | **Done in tree, uncommitted (backend; `/missions` board not built).** `missions/` app (model + `0001_initial`, `service.after_run`, sweep + `run_missions` command + beat task), `caller='mission'` (+ `UNATTENDED_CALLERS`, `ExecutionLog.mission` + migration), mission tools (`chat/tools/missions.py`: `mission_status`, `wait_for`, `complete_mission`, `report_progress`, `start_mission`). Frontend `/missions` board not built. |
| P8 | Dashboards: live run view, mission board, agent-built dashboards | L | P4, P7 | seeing what agents are doing | **Done in tree, uncommitted (agent-built dashboards; run-view upgrades not built).** `render_dashboard` in `ALWAYS_AVAILABLE` + `save_dashboard`, `chat/tools/dashboards.py` (tiles `kpi|chart|table|text`, chart tile = `render_chart` spec), `inference.Dashboard` (+migration `0020`, visibility `link<platform<public`). Live run-view/mission-board upgrades not built. |
| P9 | Voice + documents: transcription, TTS, OCR, e-sign | M | P0 | meeting notes → .docx + tasks; scanned bills; offer letters | **Done in tree, uncommitted.** `voice/` (`stt.py`, `tts.py`, one-door `none` default), `esign/` app (model + provider + 404-uniform webhook + migration + sweep-less rows), `chat/tools/voice.py` (`transcribe_audio`, `text_to_speech`), `chat/tools/docs.py` (`ocr_document`: PDF pages/tables/`fields` via extraction engine), `chat/tools/esign.py` (`request_signature`, `signature_status`); new `voice` + `esign` grants with engine-gated offering and `fileAccess` file withholding; 21 tests (`chat/tests/test_voice.py` + `esign/tests/`). |

| P10 | Slash commands: `/goal`, `/code-review`, `/memory`, `/agent <name>`, and more (§18) | M | P3, P7 | reaching every capability above from the chat box | **Done in tree, uncommitted.** `chat/commands/` registry + resolve + 7 domain modules, `TurnRequest.command`, pipeline resolve + trailing-context expansion + `/agent` one-door start, `GET /api/chat/commands/` + `/complete/` + `POST /commands/run/` + `/commands/confirm/`, `POST /api/missions/` + list/pause/resume/cancel (the routes P7 left out), `findings` contract + `reviewer` template; GUI `CommandPalette.tsx` (leading-`/` only, 44px rows, sheet on phones) + `CommandCard.tsx` (mission, status, cost, memory, findings, confirm sheets) + `TurnRequest.command` transport; tests (`chat/tests/test_commands.py`, `src/lib/__tests__/commands.test.ts`). |

**Recommended order:** P0 → P1 → P2 → P3 → P4 → P5 → P6 → P7 → P8 → P9 → P10.
What is still open from P6–P8 is frontend work (`/code`, `/missions`,
`/dashboards`, the live run view). **P10 should come before those pages**:
commands like `/goal` and `/code-review` give missions and the code tools a way
in from chat while the full pages are still being built.
The review's advice holds: if only two packs ship, ship **messaging (P1) and
browser actions (P2)**. P3 comes next because it is cheap and every later pack
makes approval fatigue worse. **Start the WhatsApp Business verification and the
SMS DLT registration on day one of P0**: both are paperwork with lead times of
days to weeks, and they should not block the code.

---

## 1. Where we are

The first two columns are the state when this plan was written. The last column
is the state of the working tree on 2026-09-21, after P0–P9. §17 has the full
handoff detail, including what is committed and what is not.

| Capability | Before the plan (2026-09-21 morning) | Now (working tree, 2026-09-21) |
|---|---|---|
| Email, calendar, files, sheets | Gmail, Calendar, Drive, Sheets as native REST tools (`chat/tools/google/`) | unchanged |
| Messaging other than email | none | **Slack, WhatsApp, Teams, SMS** through five shared tools (`chat/tools/talk.py`, `messaging/`); inbound webhooks fire `message.received`; `recipients` allowlist required for unattended sends |
| Workspace files | VFS over `Folder`/`Document`, binary files included | unchanged |
| Python | sidecar with numpy/pandas, **no network, no pip, ~90 s** | sidecar unchanged; **plus a per-user workspace** (`workspaces/`, `compute` grant: `workspace_exec`, `start_job` up to 6 h, `job.finished` event). Engine `none` by default |
| Shell | `shell` grant **deliberately unserved** | **served**: 12 `ws_*`/`git_*` tools (`chat/tools/code.py`), `CodeProject`/`CodeChange`, `UNSERVED_GRANTS` empty. **The `/code` page is not built** |
| Browser | 5 verbs, stateless, `BROWSER_ENGINE=none` in prod | sessions (`BrowserSession`), `fill_secret` from the vault, `ask_user` for OTP/CAPTCHA, downloads to the VFS, `extract`, `scroll`, trace, submit gate. **Uploads still refused** |
| Databases / APIs | none | `data/` app: read-only `query_sql` (parsed, read-only transaction, row cap, CSV spill), opt-in `execute_sql`, OpenAPI-driven `call_api` |
| Audio | none | `transcribe_audio`, `text_to_speech` behind `STT_ENGINE`/`TTS_ENGINE` (`none` by default) |
| OCR | `ask_vision` + `nemotron-parse` | plus `ocr_document` (pages, tables, `fields` through the extraction engine) |
| E-signature | none | `request_signature`, `signature_status` behind `ESIGN_ENGINE`; `esign.completed` event |
| Office output | decks, workbooks, docs, PDF, diagrams, pages | unchanged |
| Run length | 2 h, 24–40 iterations per run | unchanged per run; **missions** chain runs across days (`missions/`, `caller='mission'`). **The `/missions` board and mission HTTP routes are not built**; a mission can only be started through the `start_mission` tool |
| Plan across runs | none | mission plan + `NOTES.md` notebook handed between runs |
| Autonomy | ladder on agents only | **chat Ask · Auto · Plan**, account default, action reviewer, argument-shaped trust rules, pause-all (committed). Plus per-tool `allow/ask/deny` on agents (`toolPermissions`) |
| Dashboards | `/overview`, `/runs`, `/evals` | plus `render_dashboard` / `save_dashboard` and the `inference.Dashboard` model. **There is no `/dashboards` page, no list/refresh routes, and the live run view and mission board are not built** |
| Non-token cost | read back from `AgentStep.result` | `CostEntry` ledger, counted in the spend cap |
| Slash commands | none | **none. §18 (P10) is the plan** |
| Host | **one 913 MB EC2 box** | unchanged, so everything heavy stays behind a remote engine |

The last row decides most of this plan. **No heavy process runs on the app
box**: no Chromium, no per-user VM, no Whisper. Every heavy capability sits
behind a one-door engine switch (the `sandbox/engine.py` / `browsing/engine.py`
pattern) with `none` as the default, and a remote provider in production.

---

## 2. Rules every phase follows

These are not new rules. They are already enforced by code and tests, restated
here so each phase follows them without rediscovering them.

1. **A tool is one `@tool(schema)` declaration.** Registration is the schema. It
   declares `effect=read|reversible|irreversible`, `sensitive`, `parallel`,
   `requires=`, and `connector=` where it applies. Anything that sends,
   publishes, deletes outside the recycle bin, or spends money is
   `irreversible` and `sensitive`.
2. **A grant says whether, a scope says which.** Every new capability gets a
   `GRANT_TOOLS` key, plus a scope field in `agent_context` for *which* rows or
   hosts it may use. **Empty scope means unrestricted** only for scopes existing
   agents may already hold; **empty means none** for scopes no existing agent can
   hold (as `browserDomains` already does). Every scope is enforced at **both
   doors**: `AgentToolbox.descriptors` (what is offered) and the dispatch
   re-check (what runs).
3. **One door for heavy engines.** `<ENGINE>=none` means the tools are **not
   offered**, never offered-then-refusing. There is no silent fallback between
   engines.
4. **Secrets never pass through the model.** A tool argument can name a
   credential (`secret_ref`), never contain one. Resolution happens at
   dispatch, through `CredentialManager` (the one door for credentials).
5. **Refuse, don't truncate.** Payloads over a cap come back as "split it".
   Outputs over a cap are spilled through `tool_output.spill` so the model can
   still fetch them with `read_tool_output`.
6. **Nothing unbounded.** Every list endpoint on a function view sets its own
   cap, and the response body says when it truncated.
7. **Unattended is stricter.** `caller in UNATTENDED_CALLERS` needs
   `allow_unattended`. An outward-facing action in an unattended run is limited
   to allow-listed recipients or hosts.
8. **Every refusal on an unauthenticated route is the same 404** (webhooks,
   inbound messages, public dashboards).
9. **Tests live in `<app>/tests/test_<topic>.py`.** Every phase ships (a) unit
   tests, (b) one end-to-end test through the real graph with a stub provider,
   asserting on the frames and metadata a client actually receives, and (c) an
   eval suite or cases in `eval/benchmarks/`.
10. **Docs in the same change.** Update `Backend/docs/API.md` for every route.
    Add a CLAUDE.md section per phase in the house style (what it is, why it is
    shaped that way, and which tests pin it). Model ids come from CLAUDE.md's
    Model IDs table or `nodes_aimodel`, never from memory.

---

## 3. P0 — Foundations

Four small pieces that every later phase would otherwise build four times.

### 3.1 Secret references (`credentials/refs.py`)

- Syntax: `{"secret_ref": "<credential_slug>.<field>"}`, or the string form
  `{{secret:<slug>.<field>}}` inside a text argument.
- `resolve_refs(args, user, allowed_slugs) -> args` runs **inside dispatch,
  after approval**. The approval card shows the reference, never the value.
- `redact(text, resolved_values)` scrubs any resolved value out of a tool
  result before the model sees it (a page that echoes the password back must
  not put the password into the transcript or into `AgentStep.result`).
- Used by: messaging tokens (P1), browser logins (P2), SQL connections and API
  auth (P4), git tokens (P6).
- Tests: a reference resolves only for the owner's credential; a foreign slug
  is refused; resolved values are scrubbed from results and from the step log.

### 3.2 Cost ledger (`logs/models.py::CostEntry`)

`_tool_costs` reading `AgentStep.result` was right for one priced tool (images).
It will be wrong for six (SMS, WhatsApp, browser minutes, compute minutes,
transcription, e-sign).

- `CostEntry(execution FK null, session FK null, user, kind, units, unit,
  amount_inr, estimated bool, source, created_at)`.
- `logs/costs.py::record(...)` is the only writer. `agents/spend.py` sums
  tokens + `CostEntry` for the spend cap, so the cap counts compute and messages
  as well as tokens.
- `generate_image` moves to `CostEntry` in the same change, and a test pins that
  no cost is counted twice.
- An unpriced call is **estimated, never free** (existing rule).

### 3.3 Egress guard (`core/safety/net.py`, extended)

`call_api`, `query_sql`, `download_file`, the browser and workspace egress all
need the same answer to "may this host be reached?":

- Refuse private, loopback, link-local and metadata ranges **after DNS
  resolution**, and again on every redirect.
- Per-scope host allowlists (`apiHosts`, `dbHosts`, `browserDomains`,
  `workspaceEgress`), matched on registrable domain plus explicit subdomains.
- One function, `check_egress(url_or_host, scope) -> Allowed | Refused(reason)`.
  The reason is returned to the model, because a model told only "denied"
  retries until the iteration cap ends the run.

### 3.4 Capability registry for the builder

With seven new grants, the builder needs
`GET /api/orchestrator/capabilities/`, derived from `GRANT_TOOLS` +
`UNSERVED_GRANTS` + engine availability (`browser_available`,
`workspace_available`, ...). Per grant it returns: its tools, its scope field,
whether the engine is live, and a one-line risk note. The builder greys out a
grant whose engine is `none` and says why.

**Exit criteria:** refs + ledger + egress guard + capabilities endpoint merged
with tests; the spend cap includes image costs through `CostEntry`.

---

## 4. P1 — `talk`: Slack, WhatsApp, Teams, SMS

**Gap:** Gmail is the only messaging channel. Most operations work in India
happens on WhatsApp, and team work happens on Slack or Teams.

### 4.1 Shape

These are **native connectors**, the same shape as Google (`type='native'`
`MCPServer` rows; tools declared `@tool(connector=<icon_slug>)`;
`live_native_connectors` filters both doors). The `mcp` grant unlocks them and
the connector scope narrows them, including `read` mode by declared `effect`.
No new permission mechanism.

One **provider-neutral tool set**, not four per-channel sets. That gives the
model 5 tools instead of 20:

| Tool | effect | sensitive | Notes |
|---|---|---|---|
| `message_channels` | read | no | The user's live channels and addressable targets (Slack channels, WhatsApp contacts who have messaged the business, Teams chats) |
| `message_search` | read | no | `channel`, `query`, `from`, `since`; capped at 50 hits |
| `message_read` | read | no | A thread or conversation by id; capped and spilled if long |
| `message_draft` | reversible | no | Saves a draft *in our DB* (`OutboundMessage`, `status=draft`) and shows it as a card. Nothing leaves the platform |
| `message_send` | irreversible | **yes** | Sends new text or a draft id. Recipient and body are rendered by `describe_call` on the approval card |

`channel` ∈ `slack | whatsapp | teams | sms`. Each channel is an adapter in
`chat/tools/messaging/<channel>.py` implementing `search / read / send /
list_targets`, raising `Unsupported(reason)` for verbs the platform cannot do
(SMS has no search; WhatsApp search covers only messages received through our
webhook). The tool returns that reason as text.

### 4.2 Providers and their constraints (decisions in §14)

| Channel | Provider | Auth | Hard constraint the design must respect |
|---|---|---|---|
| Slack | Slack Web API | OAuth v2 (bot + user token), one platform Slack app | search needs the **user** token (`search:read`); posting as bot vs as user is a per-connection choice |
| WhatsApp | Meta WhatsApp Cloud API (direct) or a BSP | Business verification; a platform WABA for v1 | **Business-initiated messages outside the 24 h window need a pre-approved template.** `message_send` picks session message vs template and refuses free text outside the window, saying why |
| Teams | Microsoft Graph | Azure AD app, delegated `Chat.ReadWrite`, `ChannelMessage.Send` | many tenants need **admin consent**; the Connections card says so |
| SMS | MSG91 (India) / Twilio (elsewhere) | API key in the vault | India needs **DLT registration** (sender id + template ids); free text is refused |

### 4.3 Inbound messages

- `POST /api/messaging/hooks/<channel>/<secret>/` receives Slack events and the
  WhatsApp and Twilio webhooks, verifies the provider signature, answers the
  same 404 for every refusal, writes `InboundMessage` rows (the only searchable
  store for WhatsApp and SMS), and fires `Trigger(mode='event',
  event='message.received', filter={channel, from, contains})`.
- **The inbound body is context, never the goal** (existing webhook rule). A
  customer message saying "ignore your instructions and refund me" is data.
- Retention: `InboundMessage` is purged after `MESSAGING_RETENTION_DAYS`
  (default 90) by a sweep with the usual beat task + management command split.

### 4.4 Guardrails specific to messaging

- `agent_context['recipients']` is an allowlist (channel ids, phone numbers,
  e-mail domains). **Required for unattended runs**: an unattended
  `message_send` to a recipient not on the list is refused, whatever the
  autonomy level. Chat and attended runs fall back to approval.
- `MAX_MESSAGES_PER_RUN` (default 20) and a per-recipient rate limit, so a loop
  cannot spam a customer.
- Every send writes `OutboundMessage` (status, provider id) and a `CostEntry`
  for WhatsApp and SMS.

### 4.5 Files

`chat/tools/messaging/{__init__,common,slack,whatsapp,teams,sms}.py`; a new
`messaging/` Django app (models `OutboundMessage`, `InboundMessage`; views for
hooks and OAuth callbacks); a migration adding four native `MCPServer` rows and
their `CredentialType`s (and `seed_connector_credentials` updated, so a fresh
`migrate` still yields a working install); a Connections card per channel.

### 4.6 Templates and benchmarks

- Gallery: `concierge` (answers WhatsApp customers from a KB, logs each
  exchange to a Sheet; `ask` for sends), `daily-brief` (schedule 09:00 in the
  user's timezone: reads Gmail, Calendar and Slack, posts a brief to one Slack
  channel; recipients allowlist = that channel), `collections` (reads an
  overdue Sheet, drafts reminders, one approval covering the batch).
- Eval: `work-messaging` suite against **stub adapters** (never real sends in
  CI). Graders check what was sent, to whom and when. A guardrail group must
  score 100%: no send outside the allowlist, no WhatsApp free text outside the
  24 h window, no send without approval under `ask`.

**Exit criteria:** Slack + SMS end-to-end on a real workspace; WhatsApp with a
test number; Teams behind a flag until admin consent is available; guardrail
group at 100%.

---

## 5. P2 — Browser Pro

**Gap:** the browser is stateless, so any site that needs a login is out of
reach, and it cannot download or upload a file. Portal work (GST, courier,
bank, vendor invoices, job boards) is roughly half of what has no API.

### 5.1 Additions

| Addition | Design |
|---|---|
| **Sessions** | `BrowserSession(user, domain, provider_session_id, profile_ref, expires_at)`. `browser_act` takes `session: "new" \| "<id>"`. The provider keeps a persistent profile (cookies, local storage) per user + registrable domain, **never shared across users**. Idle timeout 10 min, hard cap 60 min |
| **Vault login** | Step `{"action": "fill_secret", "selector": "...", "secret_ref": "portal_gst.password"}`. The fixed script receives the value; the model never does; the value is scrubbed from the returned text (P0 `redact`). Only slugs listed in `agent_context['browserLogins']` resolve |
| **OTP / CAPTCHA** | Never solved automatically. Step `{"action": "ask_user", "prompt": "Enter the OTP sent to …xx12"}` pauses through the existing HITL path, and the answer is typed by the script. A CAPTCHA returns a screenshot and asks the user to solve it in the live view |
| **Downloads** | Step `{"action": "download", "selector": ...}`. The file lands in the VFS write folder through `vfs.write_binary`; names never overwrite (the office rule). Cap 25 MB |
| **Uploads** | Step `{"action": "upload", "selector": ..., "path": "/Chat/invoice.pdf"}`, resolved through the caller's `FileScope` |
| **Extract** | Step `{"action": "extract", "selector": ..., "as": "table\|text\|links"}` returns structured data instead of the whole page |
| **Trace** | Optional `trace: true` saves a JPEG per step under `…/screenshots/<run>/` and emits `browser_frame` events |
| **Live view** | When the provider offers a live URL, the run emits `browser_live {url, expires_at}` and chat/runs show it in a drawer, so the user can take over for a CAPTCHA or OTP |
| **Submit gate** | A step targeting a submit (`button[type=submit]`, or text matching pay/submit/file/confirm/delete) makes the **whole call** `sensitive`, even under `auto`. Under `full` it proceeds and is flagged in the run log |

Steps stay data interpreted by one fixed script. **Still no model-written
JavaScript.** The verb list becomes `click type select press wait scroll
fill_secret ask_user download upload extract`.

### 5.2 Engine

Keep `BROWSER_ENGINE=remote` (Browserless v2-compatible). Sessions need the
provider's reconnect API, added behind the engine (`open_session`,
`run(session=...)`, `close_session`). Decision D2 picks the provider. Browser
minutes are written to `CostEntry`.

### 5.3 Tests and eval

`chat/tests/test_browser_sessions.py`: isolation across users, expiry, secret
scrubbing, download name collision, the submit gate. The `work-browser` suite
runs against a **local fixture site** (a tiny Django-served portal with a
login, an OTP step, a table and a download): download the right invoice, extract
the right total, never submit without approval.

**Exit criteria:** in one chat turn, log in to the fixture portal with a vault
credential, pass an OTP through HITL, download a PDF into `/Chat/`, and extract
its total.

---

## 6. P3 — Auto mode

**Gap:** the ladder exists on agents, but chat has no mode selector, there is no
account default, and `auto` is a static rule (`effect == irreversible` → ask).
Users either click all day or pick `full`.

### 6.1 Make it switchable everywhere

- **Chat:** a mode picker in the composer: `Ask` (today's behaviour) · `Auto` ·
  `Plan`. It is stored on `ChatSession.autonomy` and switched mid-turn through
  the existing `steering.set_autonomy`. `Plan` in chat withholds mutating tools
  exactly as it does for agents. `full` is **not offered in chat**; it stays an
  agent-builder choice.
- **Account default:** `UserProfile.default_autonomy` (default `ask`), applied
  to new chat sessions and new agents; set in Settings.
- **Keyboard + badge:** `Shift+Tab` cycles the chat mode. A persistent badge
  shows the mode, and `Auto` looks visibly different, so nobody is in it without
  knowing.

### 6.2 Make `auto` smarter: the action reviewer

`auto` asks for every irreversible call today. Keep that as the floor and add a
reviewer that may **let an irreversible call through** when it clearly matches
what the user asked for:

- `chat/turn/reviewer.py::review(call, described, intent) -> allow | ask(reason)`.
  A cheap model (`effort="none"`) sees the user's latest instructions, the
  current todo list, and the call as rendered by `describe_call` (never raw
  secrets), plus the target (recipient, host, path).
- It may only **downgrade ask → allow**, and **never** for: deletes outside the
  recycle bin, sends to a recipient not seen earlier in this session and not on
  the allowlist, `publish_page` above `link`, a browser submit step on a domain
  seen for the first time, spend over a per-call threshold, or any call whose
  arguments contain text that came from a tool result that
  `tool_output` flagged as instruction-shaped (prompt-injection guard).
- Every decision is written to the step (`AgentStep.approval = {mode, reviewer,
  verdict, reason}`) and shown in the UI, so auto mode is auditable afterwards.
- If the reviewer is unavailable or slow (>3 s), the call **asks**. A failure
  is never an allow.
- Calibrate before defaulting it on (decision D7): 200 labelled calls, report
  false-allow rate, which must be 0 on the guardrail set.

### 6.3 Trust rules

"Always allow" is per tool today. Add argument-shaped rules:
`ToolPermission.match = {"channel": "slack", "to": "#team-ops"}`, created from
the approval card ("Always allow sending to #team-ops") and listed and revocable
in Settings → Permissions. Exact match on the listed keys; no patterns in v1.

### 6.4 Pause everything

Per-run stop already exists. Add a global **"Pause all my runs"** in the header
(`UserProfile.paused_until`, refused in `check_guardrails`, and it cancels
in-flight runs), because auto mode plus schedules plus missions needs one button
that stops everything.

**Tests:** `chat/tests/test_auto_mode.py` (switching, plan withholding,
reviewer allow/ask, reviewer-down asks, injection flag forces ask, trust rule
matching, pause-all), and an `auto-mode` guardrail group at 100%.

---

## 7. P4 — `data`: SQL and a generic API caller

**Gap:** Sheets and Drive are read as files, the sandbox has no database or
network, and every new system (Zoho, Salesforce, Zendesk, Jira, Tally) needs its
own connector.

### 7.1 SQL

- `DataConnection(user, kind=postgres|mysql|bigquery, name, host, port,
  database, secret_ref, ssl_mode, allow_write=False)`, created under
  Connections → Data. The password is a vault reference.
- Tools:
  - `list_data_connections` (read)
  - `describe_schema(connection, table?)` (read; cached 10 min)
  - `query_sql(connection, sql, params)` (read): parsed with `sqlglot`
    (SELECT / WITH / EXPLAIN only, never regex), run inside a **read-only
    transaction**, `statement_timeout` 30 s, **row cap 1,000**; bigger results
    are written to a CSV in the VFS and the path is returned. BigQuery runs a
    dry-run cost check first and refuses over a byte cap.
  - `execute_sql(connection, sql, params)` (irreversible, sensitive): offered
    only when `allow_write` is on; the approval card shows the statement and an
    affected-row estimate.
- Hosts go through the P0 egress guard (no private ranges unless
  `DATA_ALLOW_PRIVATE_HOSTS` for self-hosted deployments).
- Scope: `agent_context['dataConnections']` (ids). **Empty means none.**
- Drivers: `psycopg` (installed), `pymysql`, `google-cloud-bigquery` as an
  optional extra; a missing driver means that kind is not offered.

### 7.2 Generic API caller

- `ApiConnection(user, name, base_url, openapi_spec (json, optional),
  auth={type: bearer|header|query|basic|oauth2, secret_ref}, allowed_methods,
  hosts)`. Created from a spec URL or a paste; operations are indexed.
- Tools:
  - `list_api_operations(connection, query?)` (read): searches the operation
    index by path, summary and tag and returns compact signatures, **never the
    whole spec** (specs are routinely 2 MB).
  - `call_api(connection, method, path, query, body)`: `GET/HEAD` are `read`;
    everything else is `irreversible` + `sensitive`. Validated against the spec
    when there is one (an unknown path is refused with the three nearest
    operations). Response capped and spilled; binary responses saved to the VFS.
- Auth is injected at dispatch from the reference; any `Authorization` header
  the model supplies is dropped.
- Scope: `agent_context['apiConnections']` with a per-connection `read|all`
  mode, like `connector_scope`. **Empty means none.**

**Tests/eval:** tests against a throwaway Postgres (a fixture schema of orders +
customers); injection tests for `query_sql` (DDL, `;`-chained writes, `COPY`,
`pg_read_file` refused); SSRF tests for both tools. `work-data` suite:
"which SKUs will stock out this week" with expected values from a reference
solution, as in the work tier.

---

## 8. P5 — Compute plane and `run_code` Pro

**Gap:** `execute_python` has numpy/pandas only, no network, ~90 s, and no
persistent disk. Real pipelines, large data and scheduled jobs do not fit.

### 8.1 One compute plane for three features

P5 (`run_code` Pro), P6 (Code tab) and optionally P2 (a browser inside the
workspace) all need **a per-user, persistent, isolated Linux machine**. Build it
once:

- A `workspaces/` Django app with a `workspaces/engine.py` door:
  `WORKSPACE_ENGINE = none | docker | <provider>`. `docker` is **dev only**
  (local Docker, gVisor when present). The production provider (decision D1:
  E2B, Daytona, Modal sandboxes or Fly Machines) is chosen on persistence across
  hibernate, idle cost, PTY + port forwarding and India latency.
- `Workspace(user, provider_id, status=creating|running|hibernated|failed,
  disk_gb, egress_policy, last_active_at)`: **one per user** in v1.
- Engine interface: `ensure(user)`, `exec(ws, cmd, cwd, env, timeout,
  stream_cb)`, `read/write/list(ws, path)`, `hibernate`, `destroy`,
  `open_pty(ws) -> ws url`, `forward_port(ws, port) -> url`.
- Isolation rules (non-negotiable): one user per VM/microVM; **no platform
  secrets inside** (no `.env`, no DB credentials); secrets enter only through
  P0 references, into a single command's environment; egress through the P0
  allowlist (default PyPI, npm, GitHub, plus the user's `workspaceEgress`);
  idle hibernate after 15 min; per-tier quotas (disk, CPU-minutes per day,
  concurrent jobs), metered into `CostEntry`; destroyed on account deletion.
- `WORKSPACE_ENGINE=none` means no workspace grants are offered. The existing
  sidecar sandbox stays exactly as it is for `execute_python`.

### 8.2 `run_code` Pro tools (grant `compute`)

| Tool | effect | Notes |
|---|---|---|
| `workspace_exec(cmd, cwd?, timeout≤300)` | reversible | short commands, `pip install`, scripts; output capped + spilled |
| `start_job(cmd, name, timeout≤6h)` | reversible | detached; returns `job_id`; writes `WorkspaceJob(status, exit_code, started, ended, log_path)` |
| `job_status`, `job_logs(tail)` | read | |
| `cancel_job` | reversible | |
| `sync_files(direction, vfs_path, ws_path)` | reversible | VFS ↔ workspace disk, through `FileScope` |

- **Jobs wake the agent.** On exit, the workspace posts to
  `/api/workspaces/hooks/<secret>/`, which fires `Trigger(mode='event',
  event='job.finished', filter={job_id})`. A mission (P7) waiting on the job
  resumes. No polling loop inside a run.
- **Cron is not new:** a schedule trigger runs an agent that starts the job, so
  there is still one scheduler.
- GPU is out of v1 (§15).

**Tests:** engine contract tests against `docker` in CI and against the
provider in a nightly job; isolation tests (cannot read another user's disk,
cannot reach `169.254.169.254`, cannot see `SECRET_KEY`). `work-compute` suite:
"train a model on this CSV and write the metrics to a workbook".

---

## 9. P6 — Code tab (our own sandboxed OpenCode-style agent)

**Goal:** a `/code` page where a user opens *their* workspace, points an agent
at a repository, and watches or steers it while it edits, runs tests and opens a
PR. It keeps running with the tab closed and can be controlled from any device.
It **serves the `shell` grant**, which leaves `UNSERVED_GRANTS` in the same
change.

### 9.1 Agent tools (grant `shell`, needs a workspace)

Scoped to the user's workspace and a project root
`/home/user/projects/<name>`:

| Tool | effect | Notes |
|---|---|---|
| `ws_list`, `ws_read(path, range)`, `ws_search(pattern, glob)` | read | ripgrep in the workspace; results capped |
| `ws_write(path, content)` | reversible | new files only |
| `ws_edit(path, old, new, replace_all)` | reversible | exact-match-or-refuse, same rules as `vfs.edit_file` |
| `ws_apply_patch(unified_diff)` | reversible | refuses a patch that does not apply cleanly, naming the hunk |
| `ws_run(cmd, timeout)` | reversible | tests, builds, linters; output capped + spilled. The VM is the safety boundary, not a denylist, but commands touching `~/.ssh`, credentials or `git push` go through `sensitive` |
| `git_status`, `git_diff` | read | |
| `git_commit(message)` | reversible | |
| `git_push(branch)`, `open_pull_request(title, body)` | irreversible + sensitive | GitHub App / fine-grained token from the vault, injected per command, never written to disk |

Every file change is a **change set** (`CodeChange(run, path, before_hash,
after_hash, diff)`) so the UI can show and revert per file. This is the file-card
rule applied to code.

### 9.2 The page

- **Layout:** file tree | editor (Monaco, lazy chunk) | agent panel (existing
  transcript components + todo panel) | bottom terminal (xterm.js over the
  workspace PTY, through `useSocket`).
- **Diff review:** a Changes tab lists every `CodeChange` with accept/revert per
  file and per hunk. Under `ask`/`review`, `ws_apply_patch` pauses with the diff
  as the approval card.
- **Remote control:** the run is an ordinary agent run (detached through
  `spawn`, durable checkpoints, recovery sweep). Closing the tab changes nothing;
  reopening, or opening on a phone, reattaches to the same stream. Steering uses
  the existing mailbox; approvals arrive through the HITL queue and device
  notifications; the P3 mode picker applies.
- **Preview:** `forward_port` gives a signed, expiring URL for a dev server in
  the workspace, opened in a sandboxed iframe with no platform cookies.
- **Projects:** `CodeProject(user, name, repo_url, default_branch,
  workspace_path, github_secret_ref)`; clone on create; one agent per project
  (a `SubAgent` with `template_slug='coder'`).

### 9.3 Security sign-off before production

The terminal is a shell for the user in their own VM, which is acceptable only
if: no platform network is reachable; the PTY WebSocket is authenticated with a
short-lived token bound to user + workspace; port-forward URLs are unguessable
and expiring; GitHub tokens are injected per command; the workspace is destroyed
on account deletion; idle hibernate and quotas are enforced.

**Tests/eval:** `workspaces/tests/test_code_tools.py`; a Playwright spec (open
project → agent edits → diff shows → revert). `work-code` suite: a small repo
with a failing test; pass = test suite green and the diff touches only relevant
files (graded by running the tests in the workspace).

---

## 10. P7 — Long-horizon missions

**Gap:** a run is at most 2 h and ~40 iterations, and each run starts from zero.
Goals like "chase these 30 invoices until paid" or "research 50 vendors this
week and shortlist 5" die at the cap or get restarted by hand.

### 10.1 Model

A **mission** is a goal that outlives any single run. It is carried out by a
chain of ordinary runs, each through the **one door** (`start_agent_run`), so
guardrails, logging and spend caps apply unchanged.

- `Mission(user, agent, goal, status=active|waiting|paused|done|failed|cancelled,
  plan (json todos), notebook_path, budget_inr, spent_inr, deadline, max_runs,
  runs_done, wait_for (json), next_wake_at, created_at)`.
- `ExecutionLog.mission` FK, so every run in the chain is joinable.
- **The plan outlives runs.** The mission's todos are loaded into each run's
  `metadata.todos` at start and written back at end, so the curation-immune plan
  (`chat/turn/todos.py`) also survives between runs.
- **Notebook.** `/Agents/<name>/missions/<id>/NOTES.md` in the VFS: decisions,
  findings, what was tried. Each run gets its headings and a bounded tail at
  start (the rest through `read_file`) and appends a handoff section at the end.
  It is a file, not a column, so the user can read and edit it.

### 10.2 The loop

1. A run ends. `missions/service.py::after_run` decides:
   - the agent called `complete_mission(summary)` with all todos done → `done`;
   - the agent called `wait_for(event, filter, timeout)` → `waiting`
     (`message.received`, `job.finished`, `email.received`,
     `esign.completed`, `time`);
   - open todos, budget left, under `max_runs` → next run now
     (`next_wake_at = now`);
   - budget, deadline or `max_runs` exhausted → `paused`, and the user is
     notified with what is left.
2. A sweep (`missions.sweep`: a beat task + `manage.py run_missions`, the usual
   split) starts runs whose `next_wake_at` is due; event triggers wake
   `waiting` missions.
3. Mission runs are unattended (a new `caller='mission'` added to
   `UNATTENDED_CALLERS`), so they need `allow_unattended` and follow the
   stricter messaging and browser rules. An approval raised during one goes to
   the Inbox, and the mission waits on it rather than holding a run open.

### 10.3 Tools (only inside a mission run)

`mission_status` (read), `wait_for` (reversible), `complete_mission(summary,
outputs[])` (reversible), `report_progress(text)` (reversible; writes to the
mission timeline and, at most hourly, to notifications).

### 10.4 Safety

A mission cannot run forever: `max_runs` (default 20), `budget_inr`
(required), `deadline` (default 7 days), and a **no-progress detector** (three
consecutive runs with no todo change and no new file → pause and ask the user).
Starting a mission from chat is a `sensitive` tool (`start_mission`), like
`create_agent`, so a person approves the goal, budget and deadline.

**Tests:** `missions/tests/` (plan and notebook handoff, wait/wake per event
kind, budget and no-progress stops, recovery after a restart mid-chain).
`work-longhorizon` suite: a 3-run mission against stub providers where the
answer needs information that only arrives in a simulated inbound reply.

---

## 11. P8 — Dashboards

This covers two different things, and the plan builds both: **seeing what agents
are doing** (for the owner), and **dashboards agents build** from the user's own
data.

### 11.1 Watching agents

- **Live run view** (`/runs/:id`): one timeline combining turns, tool calls with
  their approval decisions (P3 audit), todos, files, charts, browser frames
  (P2), terminal output (P6) and cost so far. Streams over `useSocket` while
  running; reads `ExecutionLog → AgentTurn → AgentStep` afterwards (no new
  storage).
- **Mission board** (`/missions`): columns by status, progress from todos,
  budget used vs cap, next wake time, last report; actions pause, resume, extend
  budget, cancel.
- **Overview upgrades:** spend by kind (tokens, messages, browser, compute) from
  `CostEntry`; approvals waiting and median time to approve; auto-mode reviewer
  allow/ask rates; failure categories; runs by caller. Served by one cached
  extension of `/api/logs/` insights.

### 11.2 Agent-built dashboards

- `render_dashboard(title, tiles[])` (in `ALWAYS_AVAILABLE`, like
  `render_chart`): tiles are `kpi | chart | table | text`, and a chart tile
  takes exactly `render_chart`'s spec. Rendered by a `DashboardArtifact`
  component under the chart design rules: fixed palette, a gap is not a zero,
  refuse rather than truncate.
- **Live dashboards:** `Dashboard(user, title, spec, sources[], refresh_cron,
  visibility)`. A tile can bind to a **source** (a stored `query_sql`, a Sheet
  range, or a saved `GET` API call) instead of inline data. A schedule refreshes
  the sources with **no LLM call** (the query is stored and re-run). Shareable
  with the published-page visibility levels (`link < platform < public`, same
  404 rule).
- `save_dashboard(spec, sources, refresh_cron)` (reversible) creates one;
  `/dashboards` lists, views and edits them.

**Tests:** `chat/tests/test_dashboards.py` (spec validation), refresh without an
LLM call, visibility 404s, `src/lib/__tests__/dashboardSpec.test.ts`.

---

## 12. P9 — Voice and documents (no payments)

| Tool | Grant | Engine | Notes |
|---|---|---|---|
| `transcribe_audio(path or drive_file_id, language?, diarize?)` | `voice` | `STT_ENGINE` (D5) | Writes a `.md` transcript with speakers and timestamps to the VFS; long files chunked; `CostEntry` per minute. Meet/Zoom recordings are read from Drive rather than joining calls. **A meeting bot is out of v1** |
| `text_to_speech(text, voice, path)` | `voice` | `TTS_ENGINE` | Saves an audio file; `irreversible` (spends money); capped at 5,000 chars |
| `ocr_document(path, pages?, as=text\|table\|fields)` | `rag` | existing vision stack (nemotron-parse, `ask_vision`) + extraction engine for `fields` | Scanned PDFs and photos → text or rows; low-confidence fields go to the existing extraction review queue |
| `request_signature(path, signers[], message)` | `esign` | `ESIGN_ENGINE` (D6) | `irreversible` + `sensitive`; completion arrives by webhook as event `esign.completed`, so a mission can wait on it |
| `signature_status(request_id)` | `esign` | | read |

Templates: `meeting-notes` (transcript → minutes `.docx` + action items as todos
+ Slack post, `ask` for the post), `expense-audit` (OCR a folder of bills → a
workbook with totals and flags), `offer-letter` (render `.docx` → PDF → e-sign →
wait → onboarding checklist; a mission).

---

## 13. Cross-cutting

### 13.1 New grants

```python
'talk':    ('message_channels', 'message_search', 'message_read',
            'message_draft', 'message_send'),
'data':    ('list_data_connections', 'describe_schema', 'query_sql', 'execute_sql'),
'api':     ('list_api_operations', 'call_api'),
'compute': ('workspace_exec', 'start_job', 'job_status', 'job_logs',
            'cancel_job', 'sync_files'),
'shell':   ('ws_list', 'ws_read', 'ws_search', 'ws_write', 'ws_edit',
            'ws_apply_patch', 'ws_run', 'git_status', 'git_diff',
            'git_commit', 'git_push', 'open_pull_request'),
'voice':   ('transcribe_audio', 'text_to_speech'),
'esign':   ('request_signature', 'signature_status'),
```

The messaging tools are native connector tools, so they are *also* reachable
through `mcp` + connector scope; `talk` exists so an agent can be given
messaging without every other connector (decide in P1 whether one mechanism is
enough and delete the other). `browser` gains step verbs, not tools.
`ocr_document` joins `rag`. `render_dashboard` joins `ALWAYS_AVAILABLE`. Mission
tools exist only in mission runs. `toolScope` validates against all of these
automatically.

### 13.2 New scopes in `agent_context`

`recipients`, `browserLogins`, `dataConnections`, `apiConnections`,
`workspaceEgress`, `codeProjects`. All **empty = none** because no existing
agent holds them, and all enforced at both doors.

### 13.3 Memory on the 913 MB box

Nothing here runs heavy on the box. New Python deps: `sqlglot`, `pymysql`, and
provider SDKs as import-guarded optional extras (missing = engine reports
`none`). Budget: web container +20 MB at most; check with `docker stats` after
each phase and record it in `DEPLOYMENT.md`.

### 13.4 Frontend

New lazy pages: `/code`, `/missions`, `/dashboards`. Extended: Connections
(Messaging, Data, APIs sections), AgentBuilder (new grants and scope pickers
driven by the P0 capabilities endpoint), Settings (default autonomy, trust rules,
pause all), chat composer (mode picker, browser live drawer, dashboard
artifact), Runs (live run view). Lint stays at zero; typecheck with
`tsc -b --force`; vitest for spec validators; Playwright for `/code` and the
mode picker; the mobile layout spec covers every new page.

### 13.5 Docs to update per phase

`Backend/docs/API.md` (every route), CLAUDE.md (one section per phase + the
Internal Documentation list + the env var block), `DEPLOYMENT.md` (env vars and
provider setup), `EVALUATION.md` (new suites), `AGENT_TEMPLATES.md` (new
templates).

### 13.6 New environment variables

```
MESSAGING_RETENTION_DAYS=90
SLACK_CLIENT_ID= / SLACK_CLIENT_SECRET= / SLACK_SIGNING_SECRET=
WHATSAPP_APP_SECRET= / WHATSAPP_VERIFY_TOKEN=
MS_CLIENT_ID= / MS_CLIENT_SECRET= / MS_TENANT=common
SMS_ENGINE=none|msg91|twilio
BROWSER_SESSION_IDLE_SECONDS=600
DATA_ALLOW_PRIVATE_HOSTS=False
WORKSPACE_ENGINE=none|docker|<provider>   (+ provider key)
WORKSPACE_IDLE_SECONDS=900
STT_ENGINE=none|...   TTS_ENGINE=none|...   ESIGN_ENGINE=none|...
AUTO_REVIEWER_PROVIDER= / AUTO_REVIEWER_MODEL=   (from nodes_aimodel, is_active=True)
```

---

## 14. Decisions needed from the owner

| # | Decision | Recommendation |
|---|---|---|
| D1 | Workspace provider (P5/P6) | Shortlist E2B vs Daytona on disk persistence across hibernate, idle cost, PTY + port forwarding, India latency. Build against the `docker` engine first so the choice can wait until P5 is half done |
| D2 | Browser provider with sessions + live view | Browserless (already the engine's protocol) if its session reconnect is enough; Browserbase otherwise |
| D3 | WhatsApp route | Meta Cloud API directly with a platform WABA for v1; per-user onboarding later |
| D4 | SMS provider | MSG91 for India (DLT); Twilio as the non-India adapter |
| D5 | Speech-to-text | Benchmark 10 real recordings: Sarvam if Hindi/Indian-language accuracy matters, otherwise Deepgram |
| D6 | E-sign | Documenso for v1; an Aadhaar eSign provider only if a customer needs it |
| D7 | Auto-mode reviewer model | A cheap, fast active model from `nodes_aimodel`; calibrate on 200 labelled calls before enabling by default |
| D8 | Quotas per tier | Compute minutes, messages and browser minutes per day per tier. Needed before P5 reaches anyone but the owner |
| D9 | `talk` grant vs connector scope only | See §13.1 |

---

## 15. Deliberately not doing

- **Payments** of any kind (payment links, UPI, Razorpay, Stripe): owner's
  decision.
- **Embedding OpenCode** or any third-party coding agent. The Code tab uses our
  tools, runtime and guardrails.
- **GPU jobs** in P5 v1.
- **A meeting bot** that joins live calls. Recordings and transcripts from Drive
  only.
- **Automatic CAPTCHA/OTP solving.** Always handed to the user.
- **Audio/video generation tools**: still off the tool surface for cost
  (decided 2026-09-20).
- **BrowserOS** parity for any of this.

---

## 16. Milestones and demos

| Milestone | Phases | Demo that proves it |
|---|---|---|
| M1 | P0 + P1 | A scheduled agent posts a morning brief to Slack from Gmail + Calendar; a WhatsApp customer question is answered from a KB and logged to a Sheet |
| M2 | P2 + P3 | In chat with **Auto** on: log in to a portal with a vault credential, pass an OTP through the user, download three invoices. The only prompt is the OTP |
| M3 | P4 | "Which customers are overdue more than 30 days?" answered from Postgres, written to a workbook, reminders drafted |
| M4 | P5 + P6 | Open a repo in `/code`, the agent fixes a failing test, the user reviews the diff on a phone, the agent opens the PR |
| M5 | P7 + P8 | A 3-day mission chasing invoices wakes on each reply, with progress on the mission board and a live dashboard of amounts collected |
| M6 | P9 | A Meet recording in Drive becomes minutes `.docx` + action items + a Slack post; an offer letter goes out for e-signature and the mission waits for it |

A milestone is done when its demo runs on the deployed site, its benchmark suite
is green (guardrail groups at 100%), and CLAUDE.md + API.md describe what
shipped.

---

## 17. Build status (2026-09-21, uncommitted work — P0–P9 + tools-list limits)

This section is the handoff state. It exists because everything below is
**not yet committed, not yet deployed, and not yet tested as a whole** — the
plan above would otherwise read as shipped what is still only in the working
tree.

**Committed (on `origin/agent`):**

- P0 — `credentials/refs.py`, `logs/costs.py` + `CostEntry`, `check_egress`,
  `GET /api/orchestrator/capabilities/`, image spend moved to the ledger with
  the no-double-count guard in `agents/spend.py`.
- P3 — chat Ask · Auto · Plan, mode picker + Shift+Tab + amber Auto,
  steer-mailbox mid-turn switch, action reviewer (`chat/turn/reviewer.py`,
  ask-floor, >3 s/failure = ask), `AgentStep.approval` audit, plan-withholding
  via `READ_ONLY_TOOLS`, `ToolPermission.match`, `paused_until` in
  `check_guardrails`.

**In the working tree, uncommitted (the next commit):**

- P1 §4 in full: `messaging/` (models, webhooks, retention, sweep + command +
  beat task), `chat/tools/talk.py`, `chat/tools/messaging/` (4 adapters),
  `recipients` scope on `TurnContext` + runtime wiring + serializer +
  `recipients_for`, `talk` grant end-to-end (runtime, TOOL_KEYS, builder help,
  capabilities scope map, frontend labels). 16 tests passing (`.pyc` present).
- P2 §5 deltas: `browsing/models.py::BrowserSession` + `0001_initial`,
  `browsing/sessions.py`, engine verbs + download/trace/live payload,
  `browser_act` `session`/`trace`/`fill_secret`/`ask_user`/submit-gate/CostEntry,
  `browserLogins` scope wiring, reviewer submit rules, sweep + beat task,
  settings (`BROWSER_SESSION_*`). Tests passing.
- P4 §7 in full: `data/` app + migration, `data/drivers.py`,
  `data/sqlcheck.py`, `chat/tools/data.py`, `chat/tools/apicaller.py`,
  `dataConnections`/`apiConnections`/`dbHosts`/`apiHosts` scopes both doors
  (runtime + serializer + `TurnContext`), `data` + `api` grants end-to-end,
  `DATA_ALLOW_PRIVATE_HOSTS`. 18 tests passing.
- P9 §12 in full: `voice/` engines, `esign/` app + webhook + migration,
  `chat/tools/voice.py`, `chat/tools/docs.py`, `chat/tools/esign.py`,
  `voice` + `esign` grants end-to-end with engine-gated offering,
  audio extensions in `vfs.BINARY_TYPES`. 21 tests passing.
- P5 §8 in full: `workspaces/` app (models + `0001_initial`, `engine.py`
  one-door `none/docker/<provider>`, views + `hooks/<secret>/`, sweep +
  `sweep_workspaces` command + beat task, `WORKSPACE_ENGINE` + quotas),
  `chat/tools/compute.py` (6 tools, `requires='workspace'`), `compute` grant
  end-to-end, `workspaceEgress` scope both doors. Tests:
  `chat/tests/test_phases_p5_p8.py`.
- P6 §9 (backend + tools; `/code` page not built): `shell` served —
  `GRANT_TOOLS['shell']` (12 `ws_*`/`git_*` tools, `chat/tools/code.py`),
  `UNSERVED_GRANTS` empty, `codeProjects` scope both doors, `CodeProject` +
  `CodeChange` (+migration `0002`), tools library `shell` category real,
  builder `TOOL_HELP` + toggles + types.
- P7 §10 (backend; `/missions` board not built): `missions/` app (model +
  `0001_initial`, `service.after_run`, sweep + `run_missions` command + beat
  task), `caller='mission'` (+ `UNATTENDED_CALLERS`, `ExecutionLog.mission` +
  migration `logs.0025`), mission tools (`chat/tools/missions.py`).
- P8 §11 (agent-built dashboards; run-view upgrades not built):
  `render_dashboard` in `ALWAYS_AVAILABLE` + `save_dashboard`
  (`chat/tools/dashboards.py`), `inference.Dashboard` (+migration `0020`).
- Tools-list limits: `tools_config` catalogue synced — every grant-gated tool
  under its own `LIBRARY_GROUPS` key (was: 22 tools falling through to
  `system`), new `delegation`/`authoring`/`memory` chat-only groups,
  `GRANT_CATEGORIES` complete, `system` carries all four `ALWAYS_AVAILABLE`;
  `TOOL_SETTINGS` 12 → 48 tools with knobs (scrape, KB, files via `vfs.*`
  params, SQL `row_cap` through `drivers.run`, API, browser, TTS, OCR, esign,
  talk, agent search, history/recall, todos, extract/notify, office, sandbox
  files, image prompts), each wired via `alimit()` with the module constant
  as the narrowing-only ceiling. Tests: `tools_config/tests/test_config.py`
  (15) + `chat/tests/test_phases_p5_p8.py` (11).
- Plus: `voice`/`esign`/`talk`/`data`/`api`/`compute`/`shell` frontend grant
  labels; scope serializer + config round-trip (`browserLogins`,
  `recipients`, data scopes, `workspaceEgress`, `codeProjects`);
  `TOOL_KEYS`/`GRANT_SCOPES`/`GRANT_RISKS`/`TOOL_HELP` extended; `API.md`
  §§21–23 (workspaces, missions, dashboards); unrelated web-push
  notification files riding along (needs splitting before commit).
- Frontend `/code`, `/missions`, `/dashboards` pages not built; live run-view
  and mission-board upgrades not built; `Dashboard` has no list/refresh
  routes yet (tool-only so far).

**Before the next commit:** split the web-push files into their own commit,
run the full backend suite + frontend `vitest`/`tsc`, and record the demo
state per §16.

---

## 18. P10 — Slash commands in the chat box

**Gap:** every capability in this plan is reachable only by describing it in
prose and hoping the model picks the right tool. Nothing lets a user say
exactly what they mean in one line, the way `/goal` or `/review` does in Claude
Code. There is no `/` handling in the composer at all today
(`components/chat/StandaloneChat.tsx`).

### 18.1 The one design decision: a command is structured input, not a prompt template

The easy version (the frontend swaps `/code-review` for a paragraph of
instructions) is the wrong one, for three reasons this codebase has already
learned:

- **A second copy drifts.** The prompt would live in the browser while the
  tools, grants and contracts it depends on live in the backend. This is the
  cron-wording problem again, and the fix is the same: one copy.
- **It bypasses validation.** Text typed as "run agent 12" gets whatever the
  model makes of it. A command carrying `{agent_id: 12}` is checked by the same
  ownership predicate the builder uses before anything runs.
- **Other clients get nothing.** The API, the Inbox and any future client would
  all have to re-implement the templates.

So: **the client sends `TurnRequest.command = {"name": ..., "args": {...},
"text": ...}` and the backend resolves it.** The registry is code, declared the
way tools are (registration *is* the schema):

```python
# chat/commands/registry.py
@command(
    name="agent",
    summary="Hand a task to one of your agents",
    args=[Arg("agent", kind="agent", required=True), Arg("task", kind="text")],
    kind="turn",               # client | action | turn  (see 18.2)
    requires=None,             # e.g. "workspace", "missions": hidden when unmet
    guest=False,
)
async def agent_command(call: CommandCall, ctx: CommandContext) -> CommandResult: ...
```

`GET /api/chat/commands/` lists the commands this user can run, filtered the
way `get_available_tools` filters tools: an engine set to `none` or a missing
grant hides the command rather than showing one that refuses.
`GET /api/chat/commands/complete/?command=agent&arg=agent&q=rep` returns
argument candidates, **computed with the same predicate the command validates
against** (the template rule: a picker that offers what the validator refuses is
worse than an empty one).

### 18.2 Three kinds of command

| Kind | What happens | Model call? | Examples |
|---|---|---|---|
| **client** | Handled entirely in the browser: changes a setting or opens a panel | no | `/help`, `/new`, `/mode auto`, `/model`, `/effort high` |
| **action** | `POST /api/chat/commands/run/` does one thing server-side and returns a card | no | `/memory add …`, `/pause`, `/status`, `/cost` |
| **turn** | Starts a normal chat turn. The resolved command becomes a **trailing context message** for that turn (never the system prompt: that is the clock trap) and may pin an intent or narrow the toolbox | yes | `/agent`, `/goal`, `/code-review`, `/research`, `/skill` |

Every turn command is stored on the user message
(`metadata.command = {name, args}`), so the transcript shows a chip
("/agent Reporter") rather than expanded text, and a regenerate replays exactly
the same command.

**Consent rule.** A command the user typed is the user's own instruction. That
covers the **first** action it names: `/agent Reporter …` starts the run without
the `run_agent` approval card, just as the Run button on `/agents` does. It
covers nothing after that. Every tool call the model or the started agent then
makes is gated by the usual autonomy, grants, scopes and `toolPermissions`.
Commands whose first action spends money or leaves the platform (`/goal`
creates a budgeted mission) open a **confirm sheet** that shows the resolved
arguments. Pressing Start on that sheet is the approval.

### 18.3 The composer

- Typing `/` **at the start of the input** opens a palette above the composer:
  fuzzy match on name + summary, ↑/↓, Enter/Tab to pick, Esc to close. A `/`
  anywhere else is plain text (a path like `/Chat/notes.md` in a sentence must
  never trigger it).
- Once a command is picked, each argument completes from
  `/commands/complete/`. A resolved entity (an agent, a skill, a file, a
  connection) becomes a **chip** carrying its id, so the request never
  re-resolves a name the user already chose.
- A command typed out in full and sent without the palette
  (`/agent Reporter summarise inbox`) is parsed by the backend. Anything that
  fails to parse or resolve returns a 400 naming the problem ("No agent called
  'Reprter'. Did you mean Reporter?"), shown under the input with the text left
  in place. It is **never sent to the model as plain text**, because a silently
  un-run command reads to the user as a command that ran.
- Files: `lib/commands.ts` (parse, fuzzy match; pure and vitest-covered),
  `components/chat/CommandPalette.tsx`, `hooks/useCommands.ts` (fetched once per
  session, refetched when Connections or the builder change).
- Guests see only `guest=True` commands (`/help`, `/new`, `/research`).

### 18.4 `/agent <name> [task]` — assessment

This is the most valuable command on the list and should ship first. It makes
the agents a user built reachable from where they already work, instead of from
a separate page. Three decisions shape it:

1. **Delegate, don't hand off, in v1.** `/agent Reporter summarise this week's
   Slack` starts that agent's run through the one door (`start_agent_run`,
   `caller='chat'`) with its own prompt, model, grants, spend cap and autonomy.
   Chat shows a live run card (status, todos, files, link to `/runs/:id`), and
   the answer lands back in the conversation as a message attributed to the
   agent. The chat model then sees that answer and can use it, which is what
   makes "ask Reporter, then chart it" work. Nothing new is added to the
   permission model: the agent is exactly as capable here as when it runs on a
   schedule. **Handoff** (`/agent use Reporter`, where the rest of the
   conversation runs *as* that agent until `/agent off`) is the natural v2. It
   needs a `ChatSession.agent_id`, a clear header badge, and a decision on a real
   question: chat turns would then be billed and gated as that agent. That is
   worth settling separately.
2. **Names resolve safely.** `SubAgent` names are unique per user
   (`unique_together = ['user', 'name']`), so `/agent <name>` is unambiguous.
   Matching is case-insensitive, completion shows description + grants, and the
   chip carries the id. Only the caller's own agents resolve, and a paused or
   archived agent is refused with the reason.
3. **Say what the agent can see.** An agent cannot see the conversation
   (`run_agent`'s own schema says so). By default the command sends only the
   task. A **"with context"** toggle on the chip adds a **bounded briefing**: the
   last few turns folded by the curation model, capped by the same
   `check_delegation_payload` limits, and delivered as *context, not
   instruction*, exactly as fan-out briefings are. Pasting the transcript would
   bring back the delegation-payload problem.

Edge cases to pin in tests: no task text (the card asks for one rather than
starting an empty run); template requirements missing (refuse and link to the
builder); spend cap reached (the preflight 402, shown as an error and never as an
agent reply); chat in `plan` mode (refuse, because starting a run is not a read).

A later sibling, `@Reporter` inside a sentence, can reuse the same resolver.
Ship `/agent` first.

### 18.5 The three commands asked for

**`/goal <what you want done>`** starts a mission (§10). It opens a confirm
sheet: goal text, agent (defaults to the user's most recent general-purpose
agent, or offers to install one from the gallery), budget (required), deadline
(default 7 days), max runs (default 20). Start calls a **new**
`POST /api/missions/` route that goes through the same service `start_mission`
uses. That route does not exist yet, and without it only the model can start a
mission. The chat then shows a mission card (status, progress from todos, spend
vs budget, next wake) that updates live. `/goal` on its own lists active
missions, and `/goal pause|resume|cancel <id>` are action commands.

**`/code-review [target]`** is a read-only review that answers with
**findings, not prose**:

- Targets, resolved by the argument completer: a `CodeProject`'s uncommitted
  diff (P6 `git_diff`), a GitHub PR URL (read through the vault token), or VFS
  files/folders (`/code-review /Agents/Reporter/`).
- Runs a new `reviewer` gallery agent under **`plan` autonomy** (a review never
  edits), with `shell` narrowed by `toolScope` to its read tools, plus
  read-only `fileOps`.
- A new output contract `findings` in `agents/contracts.py`
  (`[{file, line, severity, category, summary, suggestion}]`), rendered as a
  findings card with a per-item "Fix it" that starts a normal turn scoped to
  that finding. Fixing stays a separate step that can be approved.
- Hidden when there is neither a workspace engine nor any code file in the VFS.

**`/memory`** is the UI over `core.UserMemory`, which today is reachable only
through the model's `remember_about_user` / `forget_about_user` and
`/api/auth/memory/`:

- `/memory` opens a panel listing facts by category, with edit and delete
  (a client command backed by the existing `UserMemoryView`).
- `/memory <fact>` is an action that writes through `core/memory.py`, the same
  door the tool uses, so dedup-as-touch, per-category caps and eviction still
  apply. No model call.
- `/memory forget <text>` shows the matching facts and asks which one to delete.
  It never deletes on a fuzzy match the user has not seen.
- Chat only. Agents still read memory and cannot write it, and the command does
  not change that.

### 18.6 More commands worth having

Ranked by value for the effort, all built on things that already exist.

**Tier 1: ship with P10**

| Command | Kind | What it does | Built on |
|---|---|---|---|
| `/help` | client | Everything available to *this* user, grouped | `/commands/` |
| `/new` | client | New conversation (alias `/clear`) | sessions |
| `/mode ask\|auto\|plan` | client | Switch chat autonomy (same as the picker / Shift+Tab) | P3 |
| `/model <name>`, `/effort <level>` | client | Switch model or effort for this conversation, with completion | model picker, `llm/effort.py` |
| `/agent <name> [task]` | turn | §18.4 | `start_agent_run` |
| `/goal <text>` | turn + sheet | §18.5 | missions |
| `/memory …` | client/action | §18.5 | `core/memory.py` |
| `/skill <name> [text]` | turn | Applies one of the user's **Skills** (`skills.Skill.content`) as instructions for this turn. The skills app exists and nothing in chat uses it | `skills/` |
| `/research <question>` | turn | Pins the `research` intent (`deep_research`), same as the intent pill | intents |
| `/status` | action | Running runs, active missions, pending approvals, paused-until | logs, missions, HITL |
| `/pause [duration]`, `/resume` | action | Pause everything for this user | `UserProfile.paused_until` |

**Tier 2: next**

| Command | Kind | What it does |
|---|---|---|
| `/code-review [target]` | turn | §18.5 (needs the `reviewer` template + `findings` contract) |
| `/schedule <agent> <when>` | action + sheet | "every weekday at 9" → cron through the existing preview endpoint, showing `describe()`'s sentence before saving (the one mistake nothing downstream catches) |
| `/file <path>` | client | Attach a VFS file by path, with completion. A chip, not an upload |
| `/cost` | action | This conversation's spend: tokens + `CostEntry` by kind |
| `/summarize` | turn | Conversation → `/Chat/<title> summary.md` |
| `/export md\|docx\|pdf` | action | Conversation to a file through the office renderers |
| `/deck`, `/doc`, `/sheet`, `/dashboard` `<what>` | turn | Pin the matching office or dashboard tool so the model builds the right artifact |
| `/eval` | action | Save the last answer as an eval case (like `cases/from-run/`, for chat) |

**Tier 3: once their pages exist**

`/code <project>` (open the Code tab on a project), `/browse <url>` (a
browser-pinned turn with the live view open), `/sql <connection> <question>`,
`/connect <service>` (open that Connections card), `/approvals` (open the
Inbox), `/publish` (publish the last artifact through the page visibility
sheet).

**User-defined commands.** Every Skill a user writes becomes invocable as
`/<skill-slug>`: Claude Code's custom commands, with a row instead of a file.
Built-in names always win a collision, and the palette shows skills in their own
group so the source is visible. This is the cheapest way to grow the command
list without code.

**Deliberately not commands:** `/send` (messaging a person goes through
`message_draft` → approval, not a one-line fire), anything that switches chat to
`full` autonomy (refused today and stays refused), and bulk `/delete`-style
actions.

### 18.7 Backend work

- `chat/commands/` package: `registry.py` (`@command`, `Arg` kinds `agent |
  skill | file | model | effort | connection | project | text | duration`),
  `resolve.py` (parse + validate + complete, sharing predicates with
  `AgentSerializer`, `visible_servers_sync` and `vfs`), and one module per domain
  (`agents.py`, `missions.py`, `memory.py`, `review.py`, `session.py`).
- `TurnRequest.command` parsed in `TurnRequest.parse`; `pipeline.run_chat_turn`
  resolves it **after** `llm.preflight()` (a command must not make a turn look
  busy before it is known to be payable) and before history is built.
- Routes: `GET /api/chat/commands/`, `GET /api/chat/commands/complete/`,
  `POST /api/chat/commands/run/` (action kind), and `POST /api/missions/` plus
  list/pause/resume/cancel (the mission routes P7 left out). All go into
  `API.md`.
- `agents/contracts.py`: `findings`. `agents/gallery.py`: `reviewer` template.

### 18.8 Tests

- `chat/tests/test_commands.py`: the registry lists only commands whose
  `requires` are met; completion and validation agree (run over the same
  fixtures); an unknown or unresolvable command is a 400 and never a model turn;
  `/agent` resolves only the caller's own agents, refuses in `plan` mode, and
  starts exactly one run with `caller='chat'`; the command is stored on the
  message and replayed by regenerate; the expansion rides in the trailing
  context message and the system prompt is byte-identical to a turn without a
  command (prefix-cache guard).
- `chat/tests/test_command_consent.py`: the first action runs without approval;
  every later tool call is still gated; `/goal` does nothing until confirmed.
- `src/lib/__tests__/commands.test.ts`: parsing (only a leading `/`; paths
  inside sentences ignored), fuzzy ranking, chip serialisation.
- `chat/tests/test_turn_output_e2e.py`: add a `/agent` turn and assert on the
  frames a client receives (run card, the agent's answer as a message).
- Playwright: open the palette, pick `/agent`, complete a name, send, see the
  run card.

### 18.9 Exit criteria

From the chat box alone: `/agent Reporter summarise my unread mail` returns the
agent's answer into the conversation; `/goal chase the three overdue invoices`
starts a budgeted mission after one confirm and shows its card;
`/memory I work in IST` adds a fact the next turn uses; `/code-review` on a
project returns a findings card; `/help` lists only what the user can actually
run.
