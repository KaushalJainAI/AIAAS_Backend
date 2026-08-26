# Extraction Merge Plan — extraction/ into inference/

> Status: **Proposed — 2026-08-18**. Owner: backend + frontend + docs sweep, one change.
> Decision: the `extraction` app is folded into `inference`; the extraction *engine* becomes an
> LLM-driven Celery task; the schema + review-queue *governance* survives as first-class models;
> the standalone `/extract` frontend page is removed (schema admin folds into Documents;
> the review queue surfaces in the Inbox).

---

## 1. Why

- **Documents already live in `inference`.** `ExtractedRow.document` already FKs `inference.Document`.
  The app boundary is crossed today — it is a leak in the taxonomy, not a real seam.
- **The extraction engine is commodity.** An LLM handler with a structured-output schema filling
  fields with per-field confidence is a solved problem (the repo already runs vision-capable LLMs
  for the chat witness; the same handler registry drives extraction). The engine should be a
  *task*, not an app.
- **Same lifecycle trigger.** A document arrives → chunk/embed for search *and* extract fields against
  schemas. One ingestion pipeline, two downstream consumers (RAG index, record store). Splitting them
  today means the extraction half simply never runs.
- **Platform direction is consolidation.** datasets deleted, evals/tuning deleted, DAG retired, nodes
  trimmed to a substrate. "Document intelligence in one app" is the same move.
- **What must NOT die in the merge:** the governance — schemas, per-schema thresholds, per-field
  confidence, `apply_threshold` invariants, the audited review queue (`reviewed_by`/`reviewed_at`),
  terminal human decisions. That is the moat; it moves, it does not dissolve.

---

## 2. Current state (recap)

| | extraction/ | inference/ |
|---|---|---|
| Models | `ExtractionSchema`, `ExtractedRow` (1 migration) | `KnowledgeBase`, `Document`, chunks (8 migrations) |
| Engine | **none** — rows only enter via API or seed script | FAISS RAG, chunking, embedder |
| Async | none | `tasks.py`, `migration_tasks.py` (Celery re-index) |
| Routes | `/api/extraction/schemas|rows` (+ nested `rows` sub-resource, `review` action) | `/api/inference/kbs|documents|rag` |
| Tests | 12 (passing) | ~15 |
| Frontend | `/extract`, `/extract/:id` pages + sidebar "Extract" item | `/documents` page |

The FK `ExtractedRow.document → inference.Document` already connects them; nothing ever
populates it from the inference side (or any side).

---

## 3. Target architecture (backend)

### 3.1 Models — move into `inference/models.py`

- `ExtractionSchema` and `ExtractedRow` move verbatim into `inference/models.py` (or, if the file
  grows past ~400 lines, an `inference/models/extraction.py` split — decide at implementation time;
  the app is currently flat).
- Models take natural `inference_extraction*` table names — **no `db_table` straitjacket**. Keeping
  the old physical names would leak a retired app's name into the schema forever; the repo values
  clean names.
- `ExtractedRow.schema` keeps `related_name='rows'`; `document` FK unchanged.

### 3.2 Merge migration (`inference/migrations/0009_merge_extraction.py`)

**Default: drop-and-reseed.** The only data that exists today is seed data produced by
`seed_improve.py` — there is nothing worth preserving. So:

1. `CreateModel` for both models with natural `inference_extraction*` names (plain, unguarded —
   fresh installs and dev DBs both work).
2. In the same migration, `RunPython` that deletes the old `extraction_extraction*` tables if they
   exist and clears `django_migrations` rows for `app='extraction'` (guarded with
   `table_exists()` — a fresh install has neither). This mirrors what the datasets removal did
   manually on the dev DB.
3. Re-run `seed_improve.py` after migrating to repopulate the demo schema + rows.

**If a production DB ever carries real extraction data (not today):** do not drop. Write a
data-move migration instead — `CreateModel` (new tables) → `RunPython` copy rows → drop old
tables → clear old `django_migrations` rows. Unambiguous, works on any DB, and leaves no
`db_table` behind. The doc commits to this branch, not a hedge.

### 3.3 Tasks — the engine (`inference/tasks.py`)

```
extract_documents(document_ids: list[int], schema_id: int, user_id: int) -> row_stats
```

- For each document: call an LLM handler directly (**`llm/handlers/` with a structured-output
  schema** — *not* the vision witness in `chat/vision/`, which is a conversational witness built
  for the chat turn loop, not a batch extractor). Vision-capable models for image documents,
  text-capable for text/PDF; the handler is resolved through the same registry the chat agent
  uses, so the user's configured provider applies.
- Output contract: the model receives the schema's field list and returns
  `{field_name: {value, confidence}}` — **per-field confidence is required from the model**, not
  computed after the fact. Anything below `schema.confidence_threshold` shows up as `needs_review`
  via the existing `apply_threshold()` invariant. No separate threshold logic in the task.
- Validation before write: unknown field names → reject the whole run (mirrors the review action's
  field check); document must belong to the user (owner check at task start).
- Idempotency: re-running `extract_documents` on the same (document, schema) **replaces** that
  document's rows (delete + recreate) rather than appending duplicates — a re-extract after a
  schema edit must not pile up stale rows. Reviewed/rejected rows are never silently overwritten:
  a re-run only replaces rows that are still `accepted`/`needs_review`; touched human-decided rows
  are left and reported in the return stats. Known wobble: a `needs_review` row may have been
  *opened* by a human who hasn't decided yet — replacing it loses the "someone already saw this"
  signal. Acceptable for phase 1; if it bites, add a `seen_at` timestamp on the row so replace
  logic can respect it.
- **Dispatch path (sync vs async) must mirror `agents/agent/runtime.py` + `start_agent_run`**:
  when `RUN_WORKFLOWS_ASYNC=True` (or Celery is reachable), the frontend-triggered POST enqueues
  `extract_documents` and returns `202 {task_id}`; otherwise it runs synchronously in the request
  cycle and returns the row stats directly. Local dev has no Redis — a Celery-only path would
  make the button silently do nothing. The `manage.py run_extraction` command covers the
  schedule-style invocation (same split as reminders/triggers).
- Registered with Celery like `migration_tasks` (autodiscover already covers `tasks.py`).
- Trigger: explicit from the frontend (button per document/schema) in phase 1; automatic on ingest
  is explicitly out of scope for phase 1.

### 3.4 Views + routes — API stays stable

- `ExtractionSchemaViewSet` / `ExtractedRowViewSet` (with the `rows` sub-resource and `review`
  action) move to `inference/extraction_views.py` — same pattern as `agents/views/`, module-per-owner.
- Routes: `inference/extraction_urls.py` mounted at **`/api/extraction/`** (unchanged path — the
  frontend `api/extraction.ts` client keeps working with zero churn). `inference/urls.py` stays
  mounted at `/api/inference/`.
- `workflow_backend/urls.py`: swap `include('extraction.urls')` → `include('inference.extraction_urls')`.
- No view logic changes — the review flow (correction validation, `corrected` flag, audit stamp)
  is already correct and is ported as-is.

### 3.5 Deletions (backend)

- Delete `Backend/extraction/` (package, migration, tests).
- `INSTALLED_APPS`: remove `'extraction'`.
- `seed_improve.py`: `from inference.models import ...` for the moved models (imports unchanged
  otherwise — the extraction section keeps seeding schemas + rows, now against the merged app).

---

## 4. Phase 2 — normalize + validate pass (separate change, designed now)

The engine produces raw strings ("₹48,200"); Phase 2 turns them into canonical data. **Not in this
merge** — but the schema of `ExtractedRow` must not foreclose it:

- **Normalize**: currency → number, dates → ISO, GSTIN checksum validation, whitespace/OCR noise.
- **Validate arithmetically**: line items + tax == grand total; vendor exists in master.
- **Point review at logic, not just vision**: queue entries show *what the machine found wrong*
  ("line items sum ₹47,980 vs. total ₹48,200") — the reviewer adjudicates, not proofreads.
- Row model already fits: `field_confidence` per cell; a future `checks` JSON column (per-field
  validation results) can be added by migration without touching the merge.

---

## 5. Frontend — no separate extraction page

**Where does extraction live instead? The review queue and the schema admin are different
concerns and should not share a screen blindly:**

- **The review queue is "needs human attention" — the same nature as HITL approvals.** The
  platform already has a home for that: **the Inbox**. Held rows (`needs_review`) surface there
  (accept / correct / reject with the `corrected` toast), and the Inbox already aggregates
  attention-worthy items, so a user who clears HITL approvals also clears extraction holds in the
  same place. This is the stronger argument than Documents: the queue is *attention*, not *data*.
- **Schema admin (create/edit schemas, fields, threshold, source) is a configuration surface** —
  it sits naturally as an "Extraction" section/tab in **`Documents.tsx`** (the document workspace:
  "Extract from this document" per document, schema list + editor, run history). A schema is a
  rule over documents, so it belongs where documents live; the review *outcomes* are what flow to
  Inbox.
- **Delete** `src/pages/Extract.tsx`, `src/pages/ExtractionSchemaDetail.tsx`; remove
  `/extract` and `/extract/:id` routes from `App.tsx`; remove the "Extract" item from the sidebar.
- New surfaces:
  - Documents → Extraction tab: schema list (cards with row/review counts, threshold, source),
    schema editor (reuse the existing schema form UI), per-document "Extract now" button that
    POSTs to the task endpoint and polls the schema's rows — the first real producer of rows.
  - Inbox → extraction section: the review queue (filter by status, accept/correct/reject).
- `src/api/extraction.ts` **stays** (endpoint paths unchanged) — only its call sites move.
- `src/lib/extractionDisplay.ts` stays (source icons/labels shared with Documents and Inbox).
- `npm run build` must pass (tsc) after the move.

---

## 6. Data + seed

- `db.sqlite3` (dev): the merge migration itself drops the old `extraction_*` tables (guarded) and
  clears their `django_migrations` rows — nothing manual. Verify with
  `python manage.py showmigrations inference` (0009 present) and `python manage.py migrate`.
- After migrating, run `seed_improve.py` to repopulate the "Purchase invoices" schema + rows for
  the demo user (imports unchanged — models now come from `inference`).
- Fresh install: `migrate` from zero must yield the same tables (fresh-install test enforces this —
  extend `mcp_integration/tests/test_fresh_install.py`-style coverage to assert inference owns the
  extraction tables).

---

## 7. Test plan

| Suite | What |
|---|---|
| `inference/tests/test_extraction.py` | Port the 12 existing tests verbatim (schema CRUD, rows sub-resource, review action, threshold change re-sort, isolation) |
| `inference/tests/test_extract_task.py` (new) | `extract_documents` with a mocked model: happy path, unknown-field rejection, below-threshold → `needs_review`, re-run replaces stale rows, **reviewed/rejected rows survive a re-run** |
| `workflow_backend/tests` | URL wiring: `/api/extraction/...` still resolves after the swap |
| Frontend | `tsc` + `npm run build`; Documents Extraction tab and Inbox review section render |
| Migration | dev DB with extraction data → migrate → old tables dropped, new `inference_extraction*` tables created, seed re-run repopulates; fresh install → tables created |

---

## 8. Doc updates (same change)

- `docs/API.md` — extraction rows move under `inference` ownership; note the merge date; update the
  coverage table when it is next refreshed.
- `CLAUDE.md` — remove `extraction/` row from the app table; fold its description into `inference/`
  ("hierarchical RAG + extraction schemas/review queue"); update the flat-list line.
- `progress.md` — extraction section becomes part of inference; counts updated (16 apps after the
  merge; inference migrations 8 → 9 with the merge migration; tests 812 + new task tests).
- `docs/RAG_STRATEGY.md` — one-line note that extraction shares the ingestion lifecycle.

---

## 9. Rollout order

- [ ] 1. Backend: move models into `inference`, write merge migration (CreateModel + guarded drop
      of old tables), `manage.py migrate`, re-run `seed_improve.py`
- [ ] 2. Port views/serializers to `inference/extraction_views.py` + `extraction_urls.py`; rewire
      `workflow_backend/urls.py`; delete `extraction/` app; `manage.py check` clean
- [ ] 3. `extract_documents` task + dispatch (async when Celery up, sync in request otherwise) +
      `manage.py run_extraction` command
- [ ] 4. Tests: port 12 + new task tests (incl. dispatch-path tests); full `pytest` green
- [ ] 5. Frontend: Documents Extraction tab + schema editor, Inbox review section; delete
      `/extract` pages/routes/sidebar item; `npm run build` green
- [ ] 6. Docs sweep (API.md, CLAUDE.md, progress.md)
- [ ] 7. Commit (with the rest of the uncommitted refactor or separately — user's call)

---

## 10. Risks / open questions

- **Prod DB with real extraction data (not today)**: dev DBs carry seed data only, so the merge
  drops the old tables. If a deploy ever carries real rows, use the data-move branch of §3.2
  instead — verify the DB before running the migration on any environment that is not throwaway.
- **Vision cost per document**: every re-extract is a model call; the replace-don't-append rule and
  explicit trigger (no auto-ingest in phase 1) keep spend user-initiated.
- **Dispatch depends on worker availability**: if `RUN_WORKFLOWS_ASYNC` flips in production, the
  sync-in-request path must still work (or the button must say why it can't) — the dispatch helper
  is the same shape as `start_agent_run`, so the failure mode is the same one already tested there.
- **Who owns `/api/extraction/` long-term**: kept as a stable alias for the frontend client; if a
  future Documents API consolidation happens, a redirect is cheap — do not do it in this change.
- **`ExtractedRow.data` schema change (Phase 2 checks)**: additive migration only; the merge must
  not pre-empt it.