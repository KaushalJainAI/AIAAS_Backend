# Office suite plan: apps that feel like applications

**Status (2026-09-25):** planned. Work is **paused by the user's choice**; this
file is the hand-off. Part of Phase A's backend was written before the pause
and sits **uncommitted** in the Backend working tree. See
[What already exists](#what-already-exists-uncommitted).

**Decision (user, 2026-09-25):** no external office server. ONLYOFFICE and
Collabora need 2–4 GB of RAM (production has 913 MB), and ONLYOFFICE is AGPL,
while both repos are public. We use MIT/Apache editor libraries inside our own
apps, and give agents tools that edit **the same real files** the apps edit.

---

## 1. Why: the gaps found

A review with screenshots (desktop, dark mode, phone) found the apps read as
pages in an admin panel rather than as applications.

**The frame**
- Four bars sit above every document: the site top bar, the site sidebar (with
  a second "AIAAS" logo), the app title bar and the file list. The document
  gets about two-thirds of the screen.
- Only one file can be open at a time. Leaving a file with unsaved changes uses
  the browser's own `window.confirm` popup.
- There is no File menu. Rename, copy, download, export, print, move and delete
  all mean leaving for "Show in Files".
- Notepad and To Do autosave; Docs, Sheets, Slides, Code and Web Studio need a
  Save button. Undo exists only in the Whiteboard.

**The preview**
- There are three different frames for the same job: the Files side pane, the
  full-preview modal and the chat drawer. Each has different buttons, and the
  chat drawer cannot "Open in <app>".
- The preview draws a file differently from the app that opens it. A workbook
  previews as a plain table and opens as a grid; a Word file previews as an
  article and opens as a page.
- The modal dims the page but not the site sidebar or top bar.
- The modal sizes itself to the file. A deck shows every slide full-size in one
  scroll, with no slide navigation.
- The preview text says "Use Export…", but those screens only have a download
  icon.
- The Markdown preview drops bullet markers.

**The editors**
- **Docs:** a stack of separate text boxes. Bold means typing `**stars**`, with
  no toolbar for bold, italic or alignment. Images and charts are placeholders.
  The file name and the document title are two different names.
- **Sheets:** formulas are never calculated, in the app or the preview. There
  is no formatting, no multi-cell selection or fill, and rows can only be added
  at the end.
- **Slides:** editing happens in a form under the slide, not on the slide.
  Slides are reordered with arrows only, and images are placeholders.
- **Code Editor:** plain text while typing; colours appear only in a separate
  read-only view. There is no find/replace and no auto-indent.
- **Uploaded Word and PowerPoint files** open read-only.
- **Phone:** five bars leave about 60% of the screen for the document.

**Other file types** (handled in Phase F)
- PDFs use the browser's built-in viewer, which shows nothing inside a page on
  most phones.
- Old Office files (`.doc`, `.xls`, `.ppt`) are filed as new Office files,
  which the libraries cannot read.
- TIFF and HEIC images do not display in most browsers; `.mkv`, `.avi`,
  `.flac` and `.aac` often do not play.
- OpenDocument, RTF, zip and email files get only an icon and a download.

## 2. Libraries (all MIT or Apache-2.0, all support React 19)

| App | Library | Version checked | Brings |
|---|---|---|---|
| Sheets | Univer (`@univerjs/presets`, `@univerjs/preset-sheets-core`) | 1.0.2 | Grid, formula engine, formatting, number formats |
| Docs | TipTap (`@tiptap/react`, `@tiptap/starter-kit` + extensions) | 3.31 | A page you type on, with a normal toolbar |
| Code | CodeMirror 6 (`codemirror`, `@codemirror/*`) | 6.x | Colour while typing, find/replace, indent |

Refused: HyperFormula (GPLv3 unless paid), SheetJS Pro (paid), and ONLYOFFICE
code copied out of the installed desktop app (AGPL, and it only runs on top of
ONLYOFFICE's own server and converter).

Every editor library loads **lazily**, only inside its app, so the login
screen and the rest of the site do not download it (see "Routes are lazy" in
CLAUDE.md).

## 3. The one rule: the real file is the truth

Agents and people edit the same `.xlsx`, `.docx` and `.pptx` files. Each
editor's own model (the Univer snapshot, TipTap JSON, our deck spec) is a view
the backend translates to and from the real file, using the Python libraries
already in the image: openpyxl, xlsxwriter, python-docx, python-pptx and
reportlab.

Consequences:
- The stored `metadata.spec` stops being the only editable thing. An uploaded
  Word or PowerPoint file is **imported** into a spec (best effort, and labelled
  as a conversion), so nothing opens read-only.
- Agent tools and app saves go through the same backend functions, so neither
  can write a shape the other cannot read.

---

## 4. Phases

Each phase is shippable on its own. Estimated size is in brackets.

### Phase A: the frame and the safety net [large]

**Backend** (mostly written; see §5)
1. `DocumentVersion` model (`inference/models.py`) and migration
   `inference/0021_document_versions.py`. It keeps the bytes (or the text) plus
   the spec.
2. `inference/versions.py`:
   - `snapshot(doc, source)`, called before every overwrite. Saves from one
     source within 5 minutes make **one** version; the last **25** versions per
     file are kept; files over 25 MB get no versions.
   - `listing`, `version_bytes`, `restore`. A restore saves the current
     contents as a version first, so it can be undone, and brings the spec back
     with the bytes.
   - Failing to keep a version never fails a save.
3. Hooks into every overwrite:
   - `office_edit.replace_bytes` and `save_text` get a `version_source`
     parameter.
   - `vfs.write_file` and `vfs.edit_file` go through `save_text`. This also
     fixes a bug: they wrote only `content_text`, so an agent's edit to an
     uploaded file never showed in downloads.
   - `vfs.write_binary(overwrite=True)` replaces the file **in place** (same
     document id, with a version) instead of trashing it and creating a new
     one, which pulled the file out from under anyone who had it open.
4. A post-delete signal removes a version's stored bytes when the version is
   pruned or purged.
5. Export (`inference/export.py` plus `inference/text_blocks.py`, a small
   Markdown/text → blocks reader):

   | From | To |
   |---|---|
   | docx | pdf, md, txt |
   | md | pdf, docx |
   | txt | pdf, docx |
   | pptx | pdf (one landscape page per slide, in the deck's colours) |
   | xlsx | csv |
   | csv | xlsx |

   The format table `FORMATS` drives the route, the File menu and the tool.
6. Routes. Each needs a row in `Backend/docs/API.md`:
   - `GET documents/<id>/versions/`: owner only.
   - `GET documents/<id>/versions/<vid>/download/`: owner only.
   - `POST documents/<id>/versions/<vid>/restore/`: owner only. Takes
     `expected_updated_at` / `If-Match`; a stale restore answers 412.
   - `GET documents/<id>/export/?to=<fmt>`: anyone who can read the file.
     Without `to`, it returns the list of formats. The parameter is **not**
     `?format=`, because DRF reserves that name and answers 404 before the view
     runs.
7. Agent tools in `chat/tools/files.py`, added to the `fileOps` grant:

   | Tool | Effect | Asks first? |
   |---|---|---|
   | `file_versions` | read | no |
   | `restore_file_version` | reversible | yes (sensitive) |
   | `export_file` | reversible; never overwrites | no |

**Frontend** (not started)
1. **`AppFrame` route.** Move `/apps/:appId` out of `<Layout />` in `App.tsx`
   into a full-screen frame. It keeps `useHITLReminders`, `useWebPush`,
   `ErrorBoundary` and `ImagineGlobalTracker`, and drops the Topbar, Sidebar and
   MobileBottomNav.
2. **One app bar** (`components/apps/AppBar.tsx`):
   - A home button and an app switcher (a grid popover of `APPS`).
   - The file name, which can be clicked to rename it (`PATCH documents/<id>/`).
   - The File menu.
   - Save status, moved here from the footer.
3. **File menu** (`components/apps/FileMenu.tsx`):

   | Item | Uses |
   |---|---|
   | New ▸ | `newFiles` |
   | Open… | a quick-open dialog |
   | Rename | `PATCH documents/<id>/` |
   | Make a copy | `copy/` |
   | Download | `download/` |
   | Export as ▸ | `export/?to=`; the formats come from the server |
   | Print | a print stylesheet, then `window.print()` |
   | Version history | the side panel (item 6) |
   | Move to… | `FolderPickerModal` + `fs/move/` |
   | Show in Files | `/documents?doc=<id>` |
   | Move to Trash | `DELETE`, then back to the app's home |

4. **Tabs.** Open files as tabs, with the list kept per app in
   `sessionStorage`, a dot on unsaved tabs, and `?file=` as the active tab.
5. **File panel.** The left list becomes a panel you can show or hide (and a
   drawer on phones). Its open/closed state is remembered.
6. **Version history panel.** Lists each version with its time and who made it
   (you, an agent, or before a restore), a preview through the version
   download, and Restore.
7. **In-app unsaved-changes dialog** ("Save / Don't save / Cancel") replaces
   `window.confirm`. Editors register a `save()` through a small context, so the
   dialog can save.
8. **Phone:** one compact bar, no bottom nav, toolbar overflow in a "⋯" menu.
9. `src/api/documents.ts`: add `versions`, `versionDownload`, `restoreVersion`,
   `exportFormats` and `exportAs`.

**Done when:** an app opens full-screen with a single bar; every File menu item
works; an agent overwrite of an open deck keeps the tab open and shows up in
history; a restore can be undone; lint is at zero; tests pass.

### Phase B: autosave and undo everywhere [medium]

- Every editor autosaves after a quiet pause (about 1.5 s) and when the tab is
  hidden or closed. The Save button goes away and the bar shows
  "Saving… / Saved".
- **Office drafts, because of the small server.** Rebuilding a `.docx`/`.pptx`/
  `.xlsx` on every autosave is too heavy for the 913 MB box.
  - Add `POST documents/<id>/draft/`. It stores the spec or grid at once
    (`metadata.draft`, cheap) and marks the file dirty.
  - The real bytes are rebuilt by a debounced background task (`spawn()`) after
    30 s of quiet, and on download, export, copy or agent read. Readers call
    `office_edit.ensure_rendered(doc)` first.
  - A draft save counts as an `app` version source, so version grouping still
    applies.
- Undo/redo in every editor for the session: Univer, TipTap and CodeMirror
  provide it; Slides and the plain-text editors need a small history stack in
  their hooks. Ctrl+Z, Ctrl+Y and Ctrl+Shift+Z work everywhere. After a reload,
  versions are the undo.
- Tests: draft → render on download; a stale draft gets 412; no more than one
  render per quiet period (a mocked clock).

### Phase C: Sheets on Univer [large]

**Backend**
- `inference/sheets.py`:
  - `to_snapshot(xlsx_bytes)` → Univer `IWorkbookData`: values, formulas,
    styles (bold, italic, colours, fill, alignment, borders), number formats,
    column widths and row heights, merges, frozen panes, sheet order and names.
  - `apply_snapshot(xlsx_bytes, snapshot)` → bytes, written with openpyxl on
    the **existing** workbook, so anything untouched survives, as
    `edit_workbook` already does.
- `inference/formulas.py`: a shared evaluator, grown from
  `eval/office_files.py` (safe AST, no `eval`). It needs:
  - Ranges and references to other sheets.
  - SUM, AVERAGE, MIN, MAX, COUNT, COUNTA, ROUND, ABS, IF, IFERROR, AND, OR,
    NOT, CONCAT/&, LEN, UPPER, LOWER, TRIM, TODAY, SUMIF, COUNTIF, AVERAGEIF,
    VLOOKUP, XLOOKUP.
  - Cycle detection.

  `eval/office_files.py` then imports it, so the benchmark and the app agree.
- `GET documents/<id>/office/` also returns calculated `values` beside the
  formulas, and previews use them.
- `office/edit.py` gains insert/delete rows and columns **mid-sheet** (formula
  references shifted, merges moved), a `format` operation, `freeze`, and
  `column_width`.
- Export: CSV uses calculated values instead of formula text.

**Tools**
- `read_workbook(path, sheet?, range?)` → values plus formulas (read).
- `edit_workbook` gains `insert_rows`, `delete_rows`, `insert_cols`,
  `delete_cols`, `format`, `freeze` and `widths`.
- `run_python_on_files` stays the escape hatch for anything bigger.

**Frontend**
- `components/apps/SheetEditor.tsx` becomes a lazy Univer mount: load the
  snapshot → Univer → the Phase B draft save → the backend applies it.
- CSV files open in the same grid and save back as CSV (values only).

**Tests:** a round trip of each feature keeps the file byte-compatible with
Excel (opened again with openpyxl); formulas shift on insert; the evaluator is
checked against a table of expected results; charts made by `render_workbook`
survive a cell edit (or, if openpyxl drops them, this is documented and the
chart is re-rendered from the spec).

### Phase D: Docs on TipTap, Slides edited on the slide [large]

**Backend**
- The document spec gains **inline runs**:
  `paragraph.runs = [{text, bold, italic, underline, strike, code, link}]`.
  The old `text` with `**markers**` is still accepted. Paragraphs and headings
  also gain `align`. Both `document.render` (python-docx) and `pdf.render`
  (reportlab) draw them.
- **Import:**
  - `inference/importers.py::docx_to_spec`: python-docx paragraphs, runs,
    heading styles, lists, tables and inline images (images saved as files
    beside the document).
  - `pptx_to_spec`: python-pptx titles, bullets, tables, images, notes, with
    each slide mapped to the nearest of our layouts.

  Triggered on first open in an app (or by `POST documents/<id>/import/`). The
  import keeps the original upload as version 1, so nothing is lost.
- **Images:** add `GET documents/<id>/asset/?path=`, which serves an image the
  spec refers to through the owner's `FileScope` (read-only). Add an "Insert
  image" upload that saves the image beside the document.

**Tools**
- `edit_document(path, ops)`: insert, replace or delete blocks by index, and
  find/replace text. It works on imported uploads.
- `edit_deck(path, ops)`: add, remove, move or duplicate slides, and set
  fields.

Both are `reversible`, save a version, and ask first like `edit_file`.

**Frontend**
- Docs: a lazy TipTap page (StarterKit, Underline, Link, TextAlign, Table,
  Image), with a toolbar for heading level, bold, italic, underline, lists,
  alignment, link, table and image. `lib/docSpec.ts` converts TipTap JSON to
  and from the spec (pure functions, with vitest tests). The title *is* the
  file name.
- Slides: edit the text directly on the slide (text fields placed over
  `DeckSlide`), drag thumbnails to reorder, a layout picker, a theme picker, and
  a notes strip under the slide. Presenter: fix the height to be measured from
  its container rather than `calc(100dvh-6rem)` (`OfficeEditors.tsx:526`,
  flagged by a peer session).

### Phase E: one preview, and the Code Editor [medium]

- **One preview frame** (`components/files/PreviewFrame.tsx`), used by the
  Files side pane, the full-preview dialog and the chat drawer. It has the same
  header everywhere: name, location, "Open in <app>", Download, Export ▸ and
  Close. The chat drawer gains "Open in <app>".
- **The preview is the app's own editor in read-only mode** (Univer read-only,
  TipTap `editable: false`, `DeckSlide` with slide navigation). A file then
  looks the same when previewed and when opened.
- Fix the modal backdrop so it covers the whole screen (portal to `body`, above
  the site bars), and give the modal a fixed size rather than one that follows
  the content.
- Fix the Markdown bullet rendering in `MarkdownMessage`'s full variant.
- Code Editor: a lazy CodeMirror 6 mount with a language per file extension,
  find/replace, bracket matching, auto-indent and the app's own light/dark
  theme. It keeps the "Format JSON" action.

### Phase F: every file type previews [small–medium]

Phases A–E fix the preview for the files people use most. These types would
still preview badly or not at all, so they are handled here. The rule: every
file shows **something useful** (its content, or a plain sentence saying why
it cannot be shown and what to do), and never zip noise or a blank box.

| Type | Problem today | Fix | New dependency? |
|---|---|---|---|
| PDF | Uses the browser's built-in viewer inside an iframe, which shows nothing on most phone browsers | **pdf.js** (Apache-2.0, lazy chunk): the same viewer on every device, with page thumbnails, search, zoom and page count. The PDF Reader app uses it too | `pdfjs-dist` (frontend) |
| Old Office: `.doc`, `.xls`, `.ppt` | `utils._EXTENSION_TYPES` files them as `docx`/`xlsx`/`pptx`, but python-docx, openpyxl and python-pptx cannot read the old binary formats, so the preview, export and the Phase D import all fail | Give them their own type (`doc_legacy`, shown as "Word 97–2003" etc.) and never pass them to the new-format code. The preview shows any text the extractor recovered (`.xls` via `xlrd` if added; otherwise none), plus: "Old Office format. Download to open, or re-save as .docx / .xlsx / .pptx to edit it here." Full conversion needs LibreOffice, i.e. the 2 GB server; noted as a follow-up, not built | optional `xlrd` (BSD) for `.xls` |
| TIFF, BMP, HEIC images | Most browsers cannot display TIFF or HEIC | `GET documents/<id>/preview-image/`: the server converts to PNG with Pillow (already installed), capped at 2400 px and cached on the row's metadata. Photos and the preview use it for these types; the download stays the original | none (`pillow-heif` only if HEIC is wanted) |
| Video `.mkv`, `.avi`; audio `.flac`, `.aac`, `.wma` | Browsers often cannot play them; today the player just stays black or silent | Try to play; on the element's `error` event, replace it with "This format doesn't play in the browser. Download it to watch." Converting media on the server is too heavy for the box, so it is not done | none |
| OpenDocument: `.odt`, `.ods`, `.odp` | Filed as `other`: an icon and a download | Add them to `_EXTENSION_TYPES` with their own types. Text extraction reads their XML inside the zip (`content.xml`), as `extract_docx_text` already does for Word, so paragraphs, table rows and slide text preview as a document, sheet table or slide list | none |
| Rich text `.rtf` | `other` | A small RTF-to-text reader (strip control words and groups), previewed as text | none |
| Zip archives `.zip` | `other` | Preview lists the files inside (name, size, date) with Python's `zipfile`; no extraction, no bytes served. Capped at 500 entries, and says so when capped | none |
| Email `.eml` | `other` | Python's `email` package: From, To, Date, Subject, the plain-text (or HTML-rendered-as-text) body, and a list of attachments by name | none |
| Anything still unknown | Icon and download | Unchanged, but with the sentence "No preview for .<ext> files" and the file size, rather than a bare icon | — |
| Very large text files | Cut at 200,000 characters, with "Show more" | Unchanged: this is right as it is | — |

**Backend**
- `inference/utils.py`: new extension mappings (`odt`/`ods`/`odp`/`rtf`/`zip`/
  `eml`, the legacy trio split out), plus extractors for each. New
  `Document.FILE_TYPE_CHOICES` values need a migration (choices only).
- `inference/previews.py` (new): `preview_image(doc)` for TIFF, BMP and HEIC,
  and `archive_listing(doc)` for zips. A route for each, readable by whoever
  can read the file, and each in `docs/API.md`.
- Existing rows keep their old type. A one-off management command
  (`manage.py retype_documents`) re-derives the type from the name for rows
  typed `other`, `docx`, `xlsx` or `pptx` whose extension says otherwise.

**Frontend**
- `lib/filePreview.ts::kindOf` gains the kinds `pdf`, `legacy_office`,
  `converted_image`, `archive`, `email` and `opendocument`, each rendered
  inside the Phase E preview frame.
- A table in `lib/__tests__/filePreview.test.ts` pins one filename per row of
  the table above to the kind it must preview as, so a new format cannot fall
  back to "icon and download" by accident.

**Tests:** one fixture file per type in `inference/tests/fixtures/`, each
checked for extracted text or listing; a TIFF returns a PNG; a zip entry never
gets served; a legacy `.doc` never reaches python-docx.

**Done when:** every type in the table shows its content or the stated
sentence, on desktop and on a phone.

### After all phases

- Rebuild the images, run the backend smoke test (import every new module,
  render one of each export), deploy, and verify on the live site. Follow the
  deploy notes in memory: build from clean worktrees, use a new dated tag, dump
  the database first, and keep the 913 MB box compose unchanged.
- Update CLAUDE.md (an entry per phase, Phase F included, as for earlier features), the app
  READMEs, `better-n8n-frontend/BEGINNER_GUIDE.md` and `Backend/docs/API.md`.

---

## 5. What already exists (uncommitted)

Written 2026-09-25 before the pause. **None of it is committed.** Other
sessions are editing the same working tree, so stage explicit paths only.

| File | State |
|---|---|
| `inference/models.py` | `DocumentVersion` + `document_version_path` added after `Document` |
| `inference/migrations/0021_document_versions.py` | created; applied to the local dev DB |
| `inference/versions.py` | new: snapshot / listing / version_bytes / restore |
| `inference/export.py` | new: `FORMATS`, `build`, workbook↔CSV, deck → PDF |
| `inference/text_blocks.py` | new: Markdown / text → document blocks |
| `inference/office_edit.py` | `replace_bytes` / `save_text` take `version_source` and snapshot first |
| `inference/vfs.py` | write_file / edit_file through `save_text`; `write_binary(overwrite)` in place via `_replace_in_place`; new `file_versions`, `restore_file_version`, `export_file` |
| `inference/signals.py` | `drop_version_blob` post-delete receiver |
| `inference/views.py` | `document_versions`, `document_version_download`, `document_version_restore`, `document_export` |
| `inference/urls.py` | the four routes above |
| `workflow_backend/thresholds.py` | `FILE_VERSIONS_KEPT`, `FILE_VERSION_COALESCE_SECONDS`, `FILE_VERSION_MAX_BYTES` |
| `chat/tools/files.py` | tools `file_versions`, `restore_file_version`, `export_file` |
| `agents/agent/runtime.py` | the three tools added to the `fileOps` grant |
| `chat/tools/describe.py` | label for `restore_file_version` |
| `inference/tests/test_versions.py` | 23 tests |

**Test state:** 22 of 23 pass. The one failure is
`ExportTests::test_the_formats_offered_are_the_formats_made`: **Word → PDF
returns 200 with an empty body inside the test client**, although
`export.build(doc, 'pdf')` returns a valid 1.6 KB PDF when called directly on
the same kind of file, and Markdown → PDF passes in the same test class.
Start by printing `Content-Length` and the response type in the test. A
suspect is the `FileResponse(BytesIO(...))` from an async view when it is the
second request on that document.

**Still to do in Phase A backend:**
- Fix the test above.
- Add the four routes to `docs/API.md`.
- Check whether any existing test pinned "overwrite trashes the old file" (run
  `chat/tests/test_office.py` and `inference/tests/`).
- Run the tool-registry parity tests (a new tool may need to be listed in
  `READ_ONLY_TOOLS` for `plan` mode: `file_versions` is read-only).
- Update the CLAUDE.md sentence that says render-tool overwrites trash the old
  file.

**To throw it away instead:** revert only the paths in the table (never
`git checkout .`, because other sessions have uncommitted work here), delete the
new files, and roll the local DB back with `manage.py migrate inference 0020`.

## 6. Risks

- **Bundle size.** Univer is several MB. It must stay a lazy chunk that loads
  only in Sheets and in the sheet preview.
- **openpyxl drops charts** it did not create when it saves a workbook. That is
  already true of `edit_workbook`, and Phase C must either keep charts or
  re-render them from the spec.
- **Import fidelity.** Converting an uploaded `.docx`/`.pptx` loses anything
  our spec cannot express. The original upload is kept as a version and the
  editor says it was converted.
- **Memory on the 913 MB box.** Rendering office files must stay debounced
  (Phase B drafts). A version keeps the full bytes of each saved state, so disk
  use grows: at most 25 per file, and nothing over 25 MB.
- **Legacy Office formats** (`.doc`, `.xls`, `.ppt`) can only be converted
  properly with LibreOffice, which needs the 2 GB server. Phase F labels them
  clearly and previews what text it can; full conversion is a follow-up.
- **Concurrent sessions.** Several Claude sessions edit this tree. Commit only
  explicit paths.
