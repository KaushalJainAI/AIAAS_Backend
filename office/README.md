# `office/`: building and reading document files

A plain Python library, **not a Django app**: no models, no views, no URLs. It
turns a *spec* (a description of slides, sheets, blocks or a chart) into a real
file, and reads files back.

It imports nothing from the rest of the backend except the number limits in
`workflow_backend.thresholds`. A test (the `office-standalone` contract in
`Backend/.importlinter`) fails if that changes.

## Who uses it

- **The AI's tools**: `chat/tools/office.py` (`render_deck`, `edit_workbook`...)
  and `chat/tools/charts.py` (`render_chart`) check the model's arguments, then
  call this library.
- **The Docs, Sheets and Slides apps**: `inference/office_edit.py`, `drafts.py`,
  `importers.py` and `export.py` save, autosave, import and export through it.
- **The benchmark**: `eval/office_files.py` reads real files back to grade them.

## Files

| File | What it does |
|---|---|
| `spec.py` | Small checks every spec uses, and `SpecError` (a message written for the model) |
| `themes.py` | The three visual themes and the chart colours |
| `charts.py` | The chart spec: which kinds exist, the limits, and `build_spec` |
| `deck.py` | Slides spec → `.pptx` (ten layouts, native editable charts) |
| `document.py` | Blocks spec → `.docx` |
| `workbook.py` | Sheets spec → `.xlsx`, with real formulas |
| `pdf.py` | Blocks spec → `.pdf` |
| `diagram.py` | Boxes and arrows → a laid-out diagram |
| `edit.py` | Changing an existing workbook in place (append rows, set cells, insert or delete rows and columns) |
| `sheets.py` | `.xlsx` ↔ the Sheets app's grid snapshot (Univer), keeping charts and anything not edited |
| `formulas.py` | The spreadsheet formula evaluator. It interprets formulas; it never runs them as Python |

## Rules

- **The model writes a spec, never code.** This library makes every visual
  decision, so every deck looks like it came from one product.
- **Limits refuse, never shrink.** A slide with too much text comes back as
  "split it", not as a tiny font.
- **Formulas stay formulas.** A total is written as `=SUM(...)`, not as a typed
  number.

Tests: `chat/tests/test_office.py`, `inference/tests/test_sheets.py`,
`inference/tests/test_formulas.py`, `eval/tests/test_office_graders.py`.
