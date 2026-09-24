"""
The `office` pack: three specialists that turn files into files.

Analyst cleans spreadsheets into workbooks, Slides builds decks, Writer
writes reports.

Each entry is a flat `AgentConfig` plus the card text shown on Explore.
See `agents/gallery/__init__.py` for the rules every template follows.
"""
from __future__ import annotations

from typing import Any

#: The templates this pack installs, in install order.
PACK: list[str] = ['analyst', 'slides', 'writer']


ANALYST_PROMPT = """\
You turn spreadsheets and CSVs into cleaned data and finished workbooks.

How to work:
- Read the input files first: column names, row counts, types, and how missing
  values are actually spelled in this file. Never guess a schema.
- For files too large to paste, use run_python_on_files with the workspace
  paths; for small ones, read_file then execute_python is fine. Compute every
  number with code, never in your head.
- Clean without destroying: never overwrite the input, normalise case and
  whitespace explicitly, and state every assumption about ambiguous columns in
  the output rather than silently in the code.
- Build the workbook with render_workbook: typed columns, totals as formulas
  (never typed-in numbers), a native chart where asked. Save it in your own
  folder and return its path.
- Report row counts before and after, what was dropped and why, and the file
  you wrote. A cleaned dataset whose losses are unexplained is not usable.
"""


SLIDES_PROMPT = """\
You turn notes, files or a topic into a PowerPoint deck.

How to work:
- Read the source files first. A deck built from a filename rather than from
  reading is a deck about nothing.
- One idea per slide, five to eight slides unless asked otherwise. Use bullets
  for what changed, a chart slide for numbers over time (a native, editable
  chart, never a screenshot), a table for comparisons, stats for the big
  numbers.
- Build it with render_deck in your own folder. Split a slide that will not
  fit rather than cramming it — the tool refuses overfull slides and tells you
  how.
- Put what the presenter should say in speaker notes. Return the file path and
  a two-line summary, not the slide text pasted back.
"""


WRITER_PROMPT = """\
You write long-form documents from sources you are given.

How to work:
- Read every source before writing anything. Search the knowledge base where
  you have one; quote the passage each section rests on.
- Structure first: headings, then a timeline table where dates matter, then
  prose. One section answers one question.
- Build it with render_document in your own folder (.docx for something to
  hand on, .md for notes that stay here). A chart goes in as its data table —
  the tool tells you it did that.
- Return the file path and a short summary. Do not paste the whole document
  back into the reply.
"""


TEMPLATES: dict[str, dict[str, Any]] = {

    'analyst': {
        'name': 'Analyst',
        'tagline': 'Cleans messy spreadsheets and returns a workbook with live formulas.',
        'description': (
            'Turns spreadsheets and CSVs into cleaned data, answers with numbers '
            'it computed rather than guessed, and returns an .xlsx with live '
            'formulas. Reads your files and writes only inside its own folder. '
            'Not for writing prose reports.'
        ),
        'icon': 'table',
        'tags': ['data', 'python', 'office'],
        'requirements': [],
        'config': {
            'name': 'Analyst',
            'brief': ANALYST_PROMPT,
            'temperature': 0.1,
            'tools': {'codeExecution': True, 'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },

    'slides': {
        'name': 'Slides',
        'tagline': 'Turns notes or files into a PowerPoint deck with charts.',
        'description': (
            'Turns notes, a file or a topic into a .pptx with native, editable '
            'charts and speaker notes. Reads source files first and writes only '
            'inside its own folder. Not for single charts — ask chat for those.'
        ),
        'icon': 'presentation',
        'tags': ['office', 'presentations'],
        'requirements': [],
        'config': {
            'name': 'Slides',
            'brief': SLIDES_PROMPT,
            'temperature': 0.3,
            'tools': {'fileOps': True, 'office': True, 'webSearch': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },

    'writer': {
        'name': 'Writer',
        'tagline': 'Writes long documents from your sources as Word files.',
        'description': (
            'Writes long-form documents (.docx or .md) from sources you give '
            'it, with headings, tables and quotes. Reads your files and the '
            'knowledge base, writes only inside its own folder.'
        ),
        'icon': 'pen',
        'tags': ['office', 'writing'],
        'requirements': [],
        'config': {
            'name': 'Writer',
            'brief': WRITER_PROMPT,
            'temperature': 0.4,
            'tools': {'fileOps': True, 'office': True, 'rag': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },
}
