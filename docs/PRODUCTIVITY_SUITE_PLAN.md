# Productivity Suite Plan — Retire BrowserOS, Build In-Browser in `better-n8n-frontend/`

Status: proposed. Scope: Backend + `better-n8n-frontend/` only. `BrowserOS/` is parked and is retired in P0, not extended.

## Goal

One productivity suite in the browser: no downloads to consume or review, open-by-type viewers, basic editors for fine human tweaks, AI does major editing, dashboards interact live. One apps launcher (`/apps`).

## Principles

1. No downloads to consume. Inline preview by default; Download is explicit Export only.
2. Server VFS is the truth (`inference/vfs.py`). No localStorage VFS, no window-manager port.
3. Open-by-type: one viewer + one light editor per kind.
4. `docs/API.md` updated in the same change as any route change.
5. `id`-addressed API only. No path-as-locator routes (`filesystem.resolve_folder` choke-point stays).

## P0 — Retire `BrowserOS/`

- Tag `archive/browseros-final`, delete `BrowserOS/` folder.
- Remove from `docker-compose.yml`, `docker-compose.prod.yml`, `Caddyfile`, `README.md` (BrowserOS section), `CLAUDE.md`, `OBJECTIVES.md`.
- Backend: delete legacy `GET /api/nodes/models/` in `Backend/llm/urls.py` + cases in `llm/tests/test_models_endpoint.py`. Update `docs/API.md` legacy-alias row. `api/browseros/`, `ws/buddy/` already gone — nothing else is BrowserOS-only.
- Do NOT port editors verbatim. Skip PixelCanvas / SceneCraft / SvgMaker / games / Terminal / Clock / Calc.

Accept: `grep -ri browseros` returns docs/history only. Frontend builds, `/api/llm/models/` is the only models path.

## P1 — Fix preview foundation

Faults today: `pdf/video/audio` icon-only (`better-n8n-frontend/src/components/documents/AuthenticatedMediaPreview.tsx`), uploaded office without spec shows text + "download for layout" (`components/files/OfficePreview.tsx`), deck images icon+path, doc charts as tables, `json/html/notebook` have no `Show more` (`components/files/FilePreview.tsx`), all saves force `a[download]`.

1. Backend `inference/views.py:document_download` (and `page_download`): add `?inline=1` serving `FileResponse(as_attachment=False)` + `Accept-Ranges`. Keep attached default.
2. Frontend media: extend `AuthenticatedMediaPreview.tsx` — `pdf -> iframe(blob)`, `video -> <video>`, `audio -> <audio>`, keep header-fetch blob pattern (never `?token=` URL).
3. Office fidelity: always fetch detail `documentsService.get(id)` for `metadata.spec` in cards / drawer / runs (pattern in `OfficePreview.tsx`). Deck image slides load via `read_image` blob; workbook charts render `ChartArtifact` from spec with a "calculated" badge instead of placeholder.
4. Truncation: `Show more` for json/html/notebook; paginate CSV past `CSV_PREVIEW_ROWS`.

Accept: pdf / video / audio / docx / xlsx / pptx all read inline. Zero forced downloads on view.

## P2 — Backend edit door

Tools `write_file` / `edit_file` / `edit_workbook` / `render_*` exist with zero HTTP routes.

- New `PATCH /api/inference/documents/<id>/content/` wrapping `vfs.write_file` (text) / `vfs.write_binary(overwrite=True)` (office re-render) / `edit_workbook.apply`. `ETag = updated_at`, `If-Match` -> 412 on stale. Reuse `resolve_documents()`, `recycle.trash` on overwrite.
- Dashboard CRUD: `GET|POST /api/inference/dashboards/`, `GET|PATCH /api/inference/dashboards/<id>/`, `POST .../refresh/`. Model (`inference/models.py:Dashboard`) + tools (`chat/tools/dashboards.py`) already save; routes are missing.
- Tests: `inference/tests/test_document_content.py`, dashboard routes test. Update `docs/API.md`.

Accept: UI edit round-trips without chat; stale edit 412s with old version in trash.

## P3 — `EditorByType.tsx`

New `better-n8n-frontend/src/components/files/EditorByType.tsx`, mounted in `DocumentPreviewModal.tsx` + `FilePreviewProvider` drawer behind `Preview|Edit` toggle, dirty dot, `Ctrl+S`:

- `md/txt -> textarea + MarkdownMessage split`; `json -> Formatted/CodeView`; `csv -> table edit -> save csv`; `html -> source + HtmlFrame (sandbox="", CSP default-src 'none')`; `xlsx -> grid append/set_cells`; `docx/deck -> blocks form -> render_document/render_deck overwrite:true`.
- No Monaco/CodeMirror v1. Binary `edit_file` refusal stays.

Accept: AI-created file opens, human fix saves in-browser, diff visible in `FileCards.tsx`.

## P4 — Dashboards interactive + Pages fix

- Frontend `/dashboards` + `/dashboards/:id` (lazy-page pattern in `App.tsx`): KPI / chart / table / text tiles via `ChartArtifact`, Refresh button, `sandbox="allow-scripts"` without `allow-same-origin` (same as `PublishedPageView.tsx`).
- `FilePage` in `PublishedPageView.tsx` gets inline preview, not Download-only.

Accept: saved dashboard re-opens live; refresh re-queries sources.

## P5 — `/apps` launcher page

New `better-n8n-frontend/src/lib/apps.ts` (same shape as `BrowserOS/src/os/apps.ts:APPS`, minus `defaultSize/multiInstance`).

- V1 only (6): Files `/documents`, Docs `/documents?kind=md,docx`, Sheets `/documents?kind=xlsx,csv`, Slides `/documents?kind=pptx`, Dashboards `/dashboards`, Pages `/pages`.
- Page `src/pages/Apps.tsx`: tile grid (icon + tint + description), `searchApps()` filter, per-tile `Open / New with AI (/ai-chat?app=) / 3 recents`. Sidebar entry (`Sidebar.tsx`, `LayoutGrid` icon). Route `/apps` in `App.tsx`.
- `Documents.tsx` deep-link `?kind=` filter; file cards default click opens drawer.

Accept: new user finds Docs / Sheets / Slides without knowing the folder tree.

## P6 — AI-drafts loop polish

- `Documents.tsx`: Recent / Starred tabs (server `updated_at` query).
- Chat / runs file cards link `/documents?doc=<id>` by id with Open button.
- Explicitly out: raster / video / svg editors, terminal, games, full Excel clone, OT-collab.

## Order

P0 -> P1 -> P2 -> P3 + P4 (parallel) -> P5 -> P6. P2 is the critical path for all editing.
