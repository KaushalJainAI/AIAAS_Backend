"""
Document formats: build, read and edit decks, workbooks, Word files, PDFs,
diagrams and charts, from a spec.

A plain Python library, not a Django app. It has no models, no views, and
imports nothing from the rest of the backend except the numeric limits in
`workflow_backend.thresholds`. Three apps use it:

* `chat/tools/office.py` and `chat/tools/charts.py`: the tools a model calls
  (`render_deck`, `edit_workbook`, `render_chart`...). They validate arguments,
  call into this package, and save the bytes into the user's files.
* `inference/`: the Docs, Sheets and Slides apps save, autosave, import and
  export through it (`office_edit.py`, `drafts.py`, `importers.py`,
  `export.py`).
* `eval/`: the benchmark grades real files by reading them back, and
  evaluates spreadsheet formulas with the same evaluator the Sheets app uses.

It used to live inside `chat/tools/office/` (and `sheets`/`formulas` inside
`inference/`), which made the file store import the chat tool folder to render
a file. Keeping it standalone means any app can produce or read a document
without depending on the chat layer, and `.importlinter` enforces that it stays
standalone.

Modules:

| Module | What it does |
|---|---|
| `spec.py` | The validation helpers every spec uses, and `SpecError` |
| `themes.py` | The three visual themes and the chart palette |
| `charts.py` | The chart spec: kinds, limits, `build_spec` |
| `deck.py`, `document.py`, `workbook.py`, `pdf.py`, `diagram.py` | Spec to `.pptx`, `.docx`, `.xlsx`, `.pdf` and diagram |
| `edit.py` | Editing an existing workbook in place (rows, cells, structure) |
| `sheets.py` | `.xlsx` to the Sheets app's grid snapshot and back |
| `formulas.py` | The spreadsheet formula evaluator (interpreted, never `eval`'d) |
"""
