# Specialist Agents Plan — tools as modules, specialists as templates

Status: **All seven phases built (2026-09-20).** M1/M2 (phases 1–4) plus the
office benchmark suite, then phases 5–7 and seven more gallery templates. The
browser (phase 7) is code-complete but **off in production until a hosted
browser is configured** (`BROWSER_ENGINE=remote`, `BROWSER_REMOTE_URL`,
`BROWSER_API_TOKEN`) — decision D3 is still the owner's. Decisions D1 and D2 were
taken as recommended (render tools are not `sensitive`; chat has them
directly). The Phase 3 file params landed as a separate `run_python_on_files`
tool rather than as params on `execute_python`, because `execute_python` is
declared `read` so `plan` mode keeps offering it — a file-writing `plan` tool
breaks that mode's whole promise.

### Progress log

- **Phase 5 — done.** `PublishedPage` (`inference.0016`/`0017`), one write path
  `inference/pages.py::publish` shared by `POST /api/inference/pages/` and the
  `publish_page` tool (`sensitive`, `irreversible`; an unattended run may only
  publish `link`), public read routes with a uniform 404 and a CSP header, and
  `?scope=platform` for the listing `platform` visibility promises. Frontend:
  `/p/:slug` (`PublishedPageView.tsx` — reports as markdown with ```chart
  fences drawn by `ChartArtifact`; HTML in an iframe sandboxed *without*
  `allow-same-origin` under a no-network CSP; files as a download) and `/pages`
  to list and withdraw. D4 resolved as: no separate origin, because HTML never
  runs with this origin. Tests: `inference/tests/test_published_pages.py`,
  `src/lib/__tests__/pageBody.test.ts`.
- **Phase 6 — done.** `generate_image` (`chat/tools/media.py`, grant `media`)
  over the Imagine OpenRouter service, saved into the file scope so decks embed
  it by path. `irreversible` (it spends money) but not `sensitive` in chat. Its
  cost reaches the spend cap without a schema change: agent runs read it back
  from the `AgentStep.result` the step already keeps (`runtime._tool_costs`),
  chat adds `metadata.tool_costs` to the turn's price. Unpriced →
  `IMAGE_COST_ESTIMATE_USD`, never free. Tests: `chat/tests/test_media.py`.
- **Phase 7 — done (7A + 7B).** `browsing/engine.py` (one door,
  `BROWSER_ENGINE=none|remote`, Browserless-compatible `/function` API) and
  `browse_page` / `browser_act` (grant `browser`). Steps are *data* for one
  fixed script, over a closed verb list. `browser_act` is scoped by
  `agent_context['browserDomains']` — empty means read-only. Deferred: handing
  screenshots to `ask_vision`, which takes chat-attachment ids, not file paths;
  screenshots are saved to the user's files instead. **Not exercised against a
  live provider** — the remote script follows the Browserless v2 `/function`
  contract and is unit-tested at the HTTP boundary only. Tests:
  `chat/tests/test_browser.py`.
- **Gallery — seven more templates:** `super-agent` (plans and delegates),
  `designer` (media + office, `ask`), `report-publisher` (research → page),
  `competitor-analysis`, `lead-research`, `content-writer`, `meeting-prep`
  (Calendar + Gmail, read-only), plus a `research` pack beside `office`.
- **Phase 3 — done.** `run_python_on_files` (`requires="files"`,
  `effect="reversible"`) over the same `sandbox.engine` as `execute_python`:
  `arun_code(code, files=, collect=)` on both engines, base64 file legs on the
  sidecar protocol, a confined `open` + temp-dir contract in the in-process
  fallback. Inputs resolve through the caller's `FileScope`, outputs save into
  its write folder via `write_binary`/`write_file` and never overwrite (a taken
  name becomes `name (2).ext`). `codeExecution` is the grant; `plan` withholds
  the new tool while keeping `execute_python`. Tests:
  `sandbox_service/tests/test_files.py`, `chat/tests/test_sandbox_files.py`.
- **Phase 4 — done.** `analyst` / `slides` / `writer` templates in
  `agents/gallery.py` (all `fileAccess: 'read_all_write_own'`, `autonomy:
  auto`, `outputContract: files`), the `files` contract in
  `agents/contracts.py` (`{summary, files}` + `/runs` file cards), `SubAgent.
  template_slug` (`orchestrator.0024`) set by `template_install`, and `POST
  /api/orchestrator/templates/install-pack/` (`{"pack": "office"}` →
  `{installed, skipped}`, idempotent, requirement-bearing templates listed as
  "needs setup"). Frontend: an "Install the office pack" card on `/templates`.
  Chat routing guidance extended in place (rule 8, not a new rule number).
  Tests: `agents/tests/test_install_pack.py` (idempotent, skips requirements,
  ownership); `test_gallery.py` covers the new templates by construction.

### Progress log

- **Phase 1 — done.** `vfs.write_binary` / `vfs.read_image` / `vfs.is_binary`;
  `Document.file_type` gained `pptx`, `xlsx` (`inference.0015`);
  `AGENT_FILE_BINARY_BYTES` (10 MB). The plan's "new `_on_file` + `Event.FILE`"
  already existed as `FILES_UPDATE` and the file cards, so the render tools
  reuse them; cards now carry `type`/`bytes` and a download button.
  `run_agent` passes the caller's write folder (`start_agent_run(workspace=)`).
  `GradeContext.binaries` + `workspace.snapshot_binaries` feed the graders.
- **Phase 2 — done.** `chat/tools/office/` (`deck`, `workbook`, `document`,
  `themes`, `spec`), grant `office`, `OfficePreview.tsx` drawing from
  `metadata.spec`. `render_document` renders charts as tables, as planned.
- **Benchmark — done, not yet run live.** `eval/office_files.py` (readers +
  formula evaluator), nine office graders, suite `work-office` on a new
  `office_worker` agent. Running it spends real tokens on the benchmark model.
- **Found on the way:** `render_chart` stored an omitted `x_label`/`note` as the
  string `"None"`, which the chat chart drew as its axis title and caption.
  Fixed in `charts._label`, test in `chat/tests/test_charts.py`.
- **Not verified visually:** no PowerPoint/LibreOffice on the dev machine, so
  deck geometry is pinned by a bounds test (every shape inside the slide) and
  by reading files back, not by looking at them.

## 1. Goal

Make the platform do what Genspark's "super agent" and Perplexity's agent
products do — produce real decks, workbooks, documents, pages and media, and
act on the web — **without adding a second orchestration system.**

The thesis, in one line: **those products are one agent loop with hands and
outputs attached.** We already have the loop, and it is the harder half —
durable runs (`chat/turn/checkpoints.py`, `agents/recovery.py`), HITL and a
five-rung autonomy ladder, spend caps, bounded delegation, steering, plans
(`update_todos`), schedules and webhooks, and per-turn observability. What is
missing is **hands** (a browser; a sandbox that can touch the user's files) and
**outputs** (binary files; shareable pages; media).

## 2. The shape: two layers, one orchestrator

| Layer | What it is | Where it lives | Cost to add one |
|---|---|---|---|
| **Capabilities** (hands) | `@tool` modules behind a grant key | `chat/tools/<domain>.py` + `GRANT_TOOLS` | Code + tests |
| **Specialists** (expertise) | A `SubAgent` config: brief, model, grants, file scope, output contract, `description` | `agents/gallery.py` template | A dict entry |
| **Orchestrator** | Chat itself, or any agent holding `subAgents` | Already exists | — |

Verified in the code (2026-09-19):

- Chat already delegates. `chat/tools/agents.py` gives it `search_agents`,
  `run_agent`, `invoke_subagent` and `get_agent_run`; routing is by the saved
  agent's `description`.
- `invoke_subagent` already passes the caller's write folder to workers as
  `FileScope.shared_prefix` (`chat/tools/agents.py:612`), so a worker called
  from chat can write into `/Chat/` and return a path.
- Installing a template writes through `AgentSerializer`, so an installed
  specialist is immediately discoverable by `search_agents`.

**Rule of thumb for routing:** the orchestrator uses cheap capability tools
itself (`render_chart`, `render_workbook` for a small table) and delegates only
work that is large, parallel, or needs a specialist's brief. Every delegation
is a full model run.

### Design rules this plan inherits (not new — restated so each phase honours them)

1. **Data, not markup.** The model sends a validated spec; our code owns every
   visual decision (`render_chart` is the precedent). Nobody asks a model to
   be a layout engine.
2. **Refuse, don't truncate.** Over a cap, return an error the model can act
   on ("fold into 'Other'", "split into two slides").
3. **Grant says whether, scope says which.** Every new capability gets a grant
   key; anything that reaches the outside world also gets a scope.
4. **Both doors.** Filter what is offered *and* re-check at dispatch.
5. **One door for runs.** No specialist gets its own runtime path.
6. **A feature that no component renders does not exist.** Every output type
   ships with its card in chat *and* on `/runs`, plus an end-to-end test on
   emitted events + stored metadata (`chat/tests/test_turn_output_e2e.py`).
7. **Stored as spec, not as picture.** Where a preview is needed, re-render it
   from the stored spec.

## 3. Gaps found while planning

| Gap | Evidence | Fixed in |
|---|---|---|
| The VFS stores text only | `vfs.write_file` writes `content_text`, `file=''`; `_EXT_TO_TYPE` has no binary types; `Document.FILE_TYPE_CHOICES` lacks pptx/xlsx | Phase 1 |
| No writer for Office files | `python-pptx` only *reads* (`chat/sources/attachments.py:57`); `xlsxwriter` is installed and unused; no `python-docx`, no `openpyxl` | Phase 2 |
| Sandbox is blind to files | `sandbox_service/server.py` accepts `code` only, no inputs/outputs; image has numpy/pandas only | Phase 3 |
| `run_agent` does not share the workspace | Only `invoke_subagent` passes `shared_prefix`; `run_agent` passes `delegation_scope` only | Phase 1 |
| Installed agents don't record their template | No `template_slug` on `SubAgent`, so "install pack" cannot be idempotent | Phase 4 |
| No public link for an output | `document_share` means "add to platform KB", not a link | Phase 5 |
| Media generation is page-only | `imagine/` has no chat tool | Phase 6 |
| No JS-capable browser | `read_url` is a static fetch + BeautifulSoup | Phase 7 |
| Graders read files as text | `GradeContext.files: dict[str, str]`, from `content_text` | Phase 1 (+ each phase) |
| Spend cap counts tokens only | `agents/spend.py::rupees_for` uses `tokens_used`; image/browser minutes are not tokens | Phase 6 |

## 4. Phases

Order: **1 → 2 → 3 → 4 → 5 → 6 → 7A → 7B**. Phases 1–4 are the first
milestone (§5): they fit the 913 MB box, need no new paid service, and produce
the headline demo.

---

### Phase 1 — Binary files in the virtual filesystem (foundation)

Everything after this writes files; this makes a file something other than text.

**Backend**
- `Document.FILE_TYPE_CHOICES` += `pptx`, `xlsx`, `png`/`jpg` (under `image`,
  which exists), `pdf` already exists. Migration (choices only).
- `inference/vfs.py::write_binary(scope, path, data: bytes, *, mime,
  text_summary, spec=None)` — same `_require_write_at` → `_make_dirs` →
  `_document_in` order as `write_file`, but saves to `Document.file` (the
  `FileField`; `MEDIA_ROOT`, the `backend_media` volume in prod) with
  `status='stored'`, and puts:
  - `content_text` = extracted text (slide titles + bullets; sheet names +
    headers + first rows). **This is what keeps `find_files` and `read_file`
    working unchanged** — both already read `content_text`.
  - `metadata['spec']` = the render spec (for previews, §7 rule 7) and
    `metadata['created_by']='agent'`.
- **Never overwrites by default.** A name collision writes `deck (2).pptx`
  and returns the real path; `overwrite: true` must be explicit. This is why
  the render tools can be `effect="reversible"` and not `sensitive` (see
  Decision D1).
- Cap: `AGENT_FILE_BINARY_BYTES` in `thresholds.py` (proposed 10 MB).
- `_EXT_TO_TYPE` += `pptx`, `xlsx`, `docx`, `png`, `jpg`, `jpeg`, `pdf`.
- `read_file` on a binary returns its `content_text` with a header line saying
  it is an extract of a binary (the `.docx`-as-zip-noise lesson: never hand
  the model bytes).
- `edit_file` on a binary is refused with "re-render it instead".
- `document_download` already serves `doc.file` when present — no change.

**Output plumbing (the "reaches nobody" guard)**
- New side effect `_on_file` in `chat/turn/agent.py`, same shape as
  `_on_chart`: `meta["files"].append({document_id, name, path, kind, size})`
  and an `Event.FILE` frame.
- `output_data['files']` on `ExecutionLog`, beside `charts` and `todos`.
- Frontend: `components/chat/FileArtifact.tsx` (icon, name, size, Download,
  "Open in Documents"), wired in `useChatStream.ts` (`case 'file'`) and
  `pages/Runs.tsx`. Previews come later per type.

**Delegation fix**
- `run_agent` passes the caller's `file_scope` through `with_shared_workspace`
  exactly as `invoke_subagent` does, so both doors return paths.

**Eval plumbing**
- `GradeContext.files` keeps `str` values (the extract), and gains
  `binaries: dict[str, bytes]` so format-aware graders can open the real file.

**Tests:** `inference/tests/test_vfs_binary.py` (scope confinement, no
overwrite, cap, extract searchable, edit refused);
`chat/tests/test_turn_output_e2e.py` extended for the `file` frame.

---

### Phase 2 — The `office` capability: decks, workbooks, documents

New module `chat/tools/office.py`, new grant `office` in `GRANT_TOOLS`,
`requires="files"` (offered only where a `FileScope` exists — chat always has
one; agents need `fileAccess` ≠ `none`). All three tools: `effect="reversible"`,
not `parallel` (file writes), run in-process in the web container
(pure-Python libraries; no LibreOffice).

**Dependencies:** add `openpyxl` (read xlsx, and pandas `read_excel`) and
`python-docx`. `python-pptx` and `xlsxwriter` are already pinned.

#### `render_workbook`
```json
{ "path": "/Chat/q3-sales.xlsx",
  "sheets": [{
    "name": "Sales",
    "columns": [{"header": "Region", "type": "text"},
                {"header": "Revenue", "type": "currency", "currency": "INR"},
                {"header": "Share", "type": "percent"}],
    "rows": [["North", 120000, "=B2/SUM(B$2:B$5)"], ...],
    "totals": true,
    "chart": {"kind": "column", "title": "Revenue by region", "x": "Region", "y": ["Revenue"]}
  }]}
```
- Formulas stay formulas (strings starting `=`) — that is the difference
  between an Excel agent and a CSV export.
- Always: bold frozen header, autofilter, column widths from content, number
  formats by column type, native Excel charts (editable) via xlsxwriter.
- Caps (refuse, don't truncate): 10 sheets, 50 columns, 5,000 rows per sheet
  (larger data goes through the sandbox in Phase 3).
- Chart kinds reuse `charts.KINDS` and `charts._series` validation so a
  workbook chart and a chat chart accept the same shapes.

#### `render_deck`
```json
{ "path": "/Chat/ev-market.pptx", "theme": "clean",
  "slides": [
    {"layout": "title", "title": "EV market in India", "subtitle": "2026 outlook"},
    {"layout": "bullets", "title": "What changed", "bullets": ["...", "..."], "notes": "..."},
    {"layout": "chart", "title": "Sales by year", "chart": { ...render_chart spec... }},
    {"layout": "image", "title": "...", "image": "/Chat/images/cover.png"},
    {"layout": "two_column", "title": "...", "left": [...], "right": [...]},
    {"layout": "table", "title": "...", "columns": [...], "rows": [...]},
    {"layout": "quote" | "section" | "closing", ...}
  ]}
```
- **Three themes, done well** (`clean`, `bold`, `dark`), each a Python table of
  fonts, sizes, colours, margins. Colours come from the validated chart
  palette. This is where the effort goes — bullet-only decks are what makes a
  deck agent look cheap.
- Native PowerPoint charts from the same chart spec (editable, not images).
- Images by VFS path (from Phase 6 or user uploads), checked against the
  *readable* scope.
- Caps: 40 slides, 6 bullets per slide, per-field character limits sized to
  the layout — over a limit returns "split this slide", never shrinks the font.
- Speaker notes supported (a presenter needs them; Genspark has them).

#### `render_document`
- Headings, paragraphs, bullet/numbered lists, tables, images, page breaks,
  a title block; one house style. Markdown-ish blocks as input, not raw
  markdown, so the tool validates structure.
- Charts in a `.docx` are **deferred** (they would need a server-side chart
  rasteriser; we have deliberately no plotting library). v1 renders the chart
  as a table + caption and says so.

**Previews**
- Deck: `DeckPreview.tsx` renders thumbnails from `metadata['spec']` in HTML
  using the same theme tokens (no server rendering). Not pixel-identical to
  PowerPoint; labelled "Preview".
- Workbook: `SheetPreview.tsx`, first 50 rows of each sheet from the spec.
- Document: rendered from the spec as a page.

**Chat availability:** chat gets `office` tools directly (its scope is fixed
to `read_all_write_own` over `/Chat/`), so "make this into a spreadsheet" does
not need a specialist.

**Tests:** `chat/tests/test_office.py` — round-trip each file through its
reader (`python-pptx`, `openpyxl`, `python-docx`) and assert structure;
formulas survive; caps refuse with a message; chart specs shared with
`render_chart`; e2e frame. Benchmark graders (§6).

---

### Phase 3 — Sandbox ↔ file tree bridge

`execute_python` gains two optional parameters:

```json
{ "code": "...", "inputs": ["/Chat/raw.csv"], "outputs": ["clean.xlsx", "summary.csv"] }
```

- **Backend (`chat/tools/sandbox.py`)**: resolve `inputs` through the caller's
  `FileScope` (readable subtree), read bytes (or `content_text` for text
  docs), send them with the code. Outputs come back as bytes and are saved
  with `vfs.write_binary`/`write_file` into the scope's *write* folder, after
  `_require_write_at`. Without a `FileScope` the parameters are refused, not
  ignored.
- **Protocol (`sandbox/engine.py::arun_code(code, files=None, collect=())`)**:
  both engines get the same envelope plus `files_out`. Engines stay the one
  door; no fallback between them.
- **Sidecar (`sandbox_service/`)**: per-run temp dir on a size-capped `tmpfs`
  (root stays read-only), becomes the cwd; inputs written in, declared outputs
  read back after exit, each capped (`MAX_FILE_BYTES`, `MAX_FILES`); names
  must be bare (no separators, no `..`). Add `openpyxl` + `xlsxwriter` to the
  sidecar requirements so pandas can read and write `.xlsx`.
  Still **no network**, still `killpg` on timeout.
- **In-process engine (dev)**: same temp-dir contract, weaker isolation, as
  today.
- Grant: needs `codeExecution` **and** a file scope; the file scope comes from
  `fileAccess`, so the existing two-axis rule holds.

**Tests:** `sandbox_service/tests/test_files.py` (caps, name rejection,
cleanup), `chat/tests/test_sandbox_files.py` (scope enforcement both ways,
outputs land in the write folder, an undeclared file is not collected).

---

### Phase 4 — Specialists as templates, and the one-click pack

**Templates** (`agents/gallery.py`). All `fileAccess: 'read_all_write_own'`
(read the user's files, write only their own folder — and a shared folder when
delegated to). Descriptions are written as routing instructions, because
`search_agents` routes on them.

| Slug | Grants | Autonomy | Description (routing) |
|---|---|---|---|
| `deep-research` (exists) | webSearch, scrape | full | Keep; add "writes findings to a markdown file when asked for a report" |
| `analyst` (upgrade of the data template) | codeExecution, fileOps, office | auto | "Turns spreadsheets and CSVs into cleaned data, answers with numbers it computed, returns an .xlsx. Not for writing prose reports." |
| `slides` | fileOps, office, webSearch | auto | "Turns notes, a file or a topic into a .pptx. Reads source files first. Not for single charts." |
| `writer` | fileOps, office, rag | auto | "Writes long-form documents (.docx or .md) from sources you give it." |
| `designer` (Phase 6) | media, fileOps | ask | "Generates images for decks and pages. Costs credits per image." |
| `browser` (Phase 7) | browser | ask/review | "Uses websites that have no API. Pauses before submitting anything." |
| `inbox-triage` (exists) | mcp | ask | Keep |

Each gets a **brief** in the style of the existing ones (how to work, what to
refuse), and an **output contract** — add `FILES` to `agents/contracts.py`
(`{"summary": str, "files": [path]}`) with its `/runs` panel, so a specialist's
result renders as file cards rather than prose mentioning paths.

**Install tracking**: `SubAgent.template_slug` (nullable, migration) set by
`template_install`, so reinstalling is detectable.

**Pack install**: `POST /api/orchestrator/templates/install-pack/`
`{"pack": "office"}` → installs every template in the pack whose requirements
are empty and which is not already installed (`template_slug`), returns
`{installed, skipped: [{slug, reason}]}`. Templates with requirements are
listed as "needs setup" and link to the normal install screen. Frontend: an
"Install the office pack" card on `/templates` and an empty-state prompt in
chat when `search_agents` finds nothing.

**Chat routing guidance** (`chat/turn/prompts.py`): extend the existing
delegation rule rather than add a rule number — "use office tools yourself for
one file; delegate to a specialist for a multi-step job (research → deck), and
pass findings via files, not via the task text."

**Approval friction**: `run_agent`/`invoke_subagent` stay `sensitive`; the
card's "allow for this session" scope already exists. The demo script clicks
it once.

**Tests:** `agents/tests/test_gallery.py` already fails a template the builder
would refuse — new templates are covered by construction;
`agents/tests/test_install_pack.py` (idempotent, skips requirements,
ownership).

---

### Phase 5 — Hosted pages (Sparkpages / Labs-style outputs)

A **snapshot** of an output at `/p/<slug>`, reusing the `SharedAgent`
decisions (`agents/publishing.py`) rather than inventing new ones:

- Model `PublishedPage` (app: `inference/`, beside documents): owner, slug,
  title, `kind` (`report` | `html` | `file`), `body` (markdown + chart specs,
  or HTML, or a document id), visibility `link < platform < public`, default
  `platform`, `withdrawn_at` (unlist, never delete).
- Tool `publish_page` — `sensitive=True`, `effect="irreversible"` (it is
  outward-facing); never in `ALWAYS_AVAILABLE`; never granted to unattended
  runs above `link` visibility without approval.
- Anonymous route: every refusal is the same 404; a separate anonymous
  projection function; capped.
- **HTML safety**: user/model HTML is served only inside a sandboxed iframe
  (`sandbox` without `allow-same-origin`) with a strict CSP, ideally from a
  separate origin — never inline on the app origin, or a published page is
  an XSS on our login cookies.
- Frontend: "Publish" on a chat answer, a chart, an HTML artifact or a file
  card; `/p/:slug` public page; a "My pages" list.

**Tests:** `inference/tests/test_published_pages.py` (404 uniformity,
visibility, snapshot not pointer, withdraw), a CSP header test.

---

### Phase 6 — Media as tools

- Tool `generate_image` (grant `media`), calling `imagine/services/dispatcher`
  so model choice, validation (`imagine/validation.py`) and the user's
  OpenRouter credential are reused. Waits up to a bound, then saves the image
  into the VFS with `write_binary` and returns its path — so `render_deck` can
  use it.
- `effect="reversible"` (it writes a new file), `sensitive` in chat is not
  needed, but **cost is real**: add `ExecutionLog.extra_cost_rupees` (or a
  cost ledger) written by the tool and summed by `agents/spend.py`, so the
  spend cap covers images. A cap that ignores what the tool actually spent is
  the `credits_used` bug again.
- Later: `generate_audio` (a podcast overview of a report) through the same
  shape.

**Tests:** spend cap refuses an image once over cap; file lands in scope;
failure surfaces as an error, not an apology (fail-fast rule).

---

### Phase 7 — Browser (the part of Perplexity's product we lack)

**Constraint:** Chromium does not fit on the 913 MB box. `BROWSER_ENGINE` =
`none` (default) | `remote`, the same one-door shape as `sandbox/engine.py`;
`remote` drives a hosted browser (Browserbase / Steel / Browserless) over CDP
with the Playwright Python client (no browser binaries in our image). No
automatic fallback.

**7A — read-only** (grant `browser`)
- `browse_page(url)` → rendered text (after JS), title, links, and a
  screenshot stored as a run-scoped image. The screenshot is handed to the
  existing **vision witness** (`ask_vision`), so "what does this page show"
  needs no new vision code.
- `effect="read"`, `parallel=True`. SSRF guard via `core/safety/net`.
- Page text is **data, never instructions** — same rule as the webhook body;
  stated in the tool description and the system prompt.

**7B — acting**
- `browser_click`, `browser_type`, `browser_select`, `browser_submit` on a
  run-owned session; all `effect="irreversible"`, so the autonomy ladder gates
  them with no new mechanism (`ask` pauses on submit; `plan` withholds them).
- **Scope:** `agent_context['browserDomains']` — the "which" for the grant,
  enforced at both doors like connectors; empty = read-only browsing only.
- Sessions are closed on every terminal path (like `forget_thread`) and are
  billed through the same cost ledger as Phase 6.
- Never types a credential the user did not supply in the run; never solves
  CAPTCHAs.

**Tests:** fake CDP engine; domain scope at both doors; ladder gating;
session closed on cancel/fail; injection text in a page does not change tool
choice (a guardrail benchmark case).

---

## 5. Milestones

| Milestone | Phases | Demo that proves it | Exit criteria |
|---|---|---|---|
| **M1 — Real outputs** | 1, 2 | "Turn this table into a spreadsheet with totals and a chart" → download a real `.xlsx` with live formulas; "make a 6-slide deck on X" → `.pptx` with native chart | Files open in Excel/PowerPoint/LibreOffice; cards render in chat and `/runs`; office benchmark suite green at 3/3 |
| **M2 — Analyst + specialists** | 3, 4 | Upload a messy CSV → "clean it and present it" → chat delegates to Analyst (xlsx) then Slides (pptx), both in `/Chat/` | Pack install idempotent; per-specialist benchmark suites at pass^3 targets |
| **M3 — Share it** | 5 | "Research X and publish it" → public `/p/<slug>` with charts | 404 uniformity + CSP tests green |
| **M4 — Media** | 6 | Deck with generated cover image | Spend cap counts image cost |
| **M5 — Browser** | 7A, 7B | "Find three flights under ₹8k on <site>" (read), then fill a form and pause before submit | Domain scope + ladder gating tests green |

## 6. Evaluation — each specialist is measured on its own

New graders in `eval/graders.py` (registration is the schema):

- `pptx_slide_count`, `pptx_contains` (text on any slide), `pptx_has_chart`,
  `pptx_notes_present`
- `xlsx_cell` (value or formula at a cell), `xlsx_formula_result` (compute with
  openpyxl-cached values or a tiny evaluator for SUM/AVERAGE only),
  `xlsx_sheet_names`, `xlsx_has_chart`
- `docx_contains`, `docx_heading_count`
- `file_type` (the file at path is really a pptx/xlsx, not text renamed)

New suites in `eval/benchmarks/suites/`: `work_office.py` (decks and
workbooks from fixtures), extend `work_analyst.py` (CSV → xlsx with formulas),
`guard_office.py` (caps refuse; no overwrite without the flag; writes outside
scope refused). Same work-tier rules: fixtures reset per attempt, expected
values computed by a reference solution, a test that fails if a case passes
with untouched fixtures, 3 runs → pass@1 and pass^3.

## 7. Cross-cutting

- **Memory on the 913 MB box:** python-pptx/openpyxl/python-docx are
  pure-Python; generation is bounded by the caps above. Measure peak RSS of a
  40-slide deck and a 5,000-row workbook in a test and record it here before
  M1 ships.
- **Disk:** binaries go to the `backend_media` volume; the recycle sweep
  already deletes the `Document` row — verify it deletes the file too
  (`FileField` does not delete on row delete by default).
- **Docs:** update `Backend/docs/API.md` for every new route (pack install,
  pages, public page), and add a CLAUDE.md section per phase in the house
  style (the decision and why).
- **Frontend:** new cards go into `components/chat/`; pages lazy-loaded;
  `npm run lint` stays at zero; `tsc -b --force`.
- **BrowserOS:** parked — not updated.

## 8. Decisions needed from the owner

| # | Decision | Recommendation |
|---|---|---|
| D1 | Are `render_*` tools `sensitive` (ask in chat before writing)? | **No**: they only create new files (no overwrite without a flag) and a file can be trashed; asking every time kills the flow. `write_file` stays sensitive. |
| D2 | Office tools always available in chat, or only via specialists? | **Always in chat**; specialists for multi-step jobs. |
| D3 | Browser provider for Phase 7 | Decide at M5; keep behind `BROWSER_ENGINE` so it is swappable. |
| D4 | Public pages on the app origin or a separate one | **Separate origin** for any HTML kind; markdown reports may stay same-origin (they are rendered by our component). |
| D5 | Auto-install the office pack for new accounts? | **No**: one-click card instead, so an account never holds agents the user did not choose. |

## 9. Deliberately not doing

- Phone calls ("call for me") — telephony + voice is a separate product.
- A Comet-style desktop browser app, and anything in BrowserOS.
- A plotting library in the sandbox, or model-written python-pptx code — one
  renderer, one design system.
- Server-side PowerPoint rendering (LibreOffice) for thumbnails — previews
  come from the spec.
- Specialists that call specialists — delegation stays one level deep.
