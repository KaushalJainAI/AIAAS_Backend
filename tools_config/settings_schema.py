"""
What a user is actually allowed to change about a tool.

Two rules keep this honest.

**A knob exists here only if a tool reads it.** The schema is not documentation
of what we might one day support — every entry below is fetched at the one line
in the tool that used to hold a constant, so a knob that appears in the UI is a
knob that moves something. `test_config.py` fails if a declared key is never
read.

**Nothing here changes behaviour, only budget.** These are ceilings: how much
text comes back, how many results, how much stdout. A setting that changed what
a tool *does* would make the tool's own description a lie, and the description
is what the model plans against.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict

from workflow_backend.thresholds import (
    DEEP_RESEARCH_CHAR_LIMIT,
    IMAGE_SEARCH_MAX_RESULTS,
    READ_URL_CHAR_LIMIT,
    SEARCH_RESULT_LIMIT,
    VIDEO_SEARCH_MAX_RESULTS,
)

#: Local defaults for knobs below. Each mirrors the module constant its tool
#: reads as the floor under a failed overlay lookup — declared once here so
#: the catalogue, the clamp range and the tool agree, instead of three copies
#: drifting. Where a thresholds.py value already exists it is imported above.
_SCRAPE_CHAR_LIMIT = 15_000
_KB_TOP_K = 5
_KB_SNIPPET_CHARS = 2_000
_DOC_LIST_CAP = 50
_READ_WINDOW_CHARS = 12_000
_FILE_LIST_LIMIT = 200
_FILE_READ_CHARS = 30_000
_FILE_WRITE_CHARS = 200_000
_SQL_ROW_CAP = 1_000
_API_OP_LIST_LIMIT = 30
_API_RESPONSE_CHARS = 32_000
_BROWSER_TEXT_CHARS = 15_000
_BROWSER_MAX_STEPS = 15
_TTS_MAX_CHARS = 5_000
_OCR_MAX_PAGES = 20
_OCR_PAGE_CHARS = 8_000
_OCR_MAX_ROWS = 200
_ESIGN_MAX_SIGNERS = 10
_TALK_SEARCH_LIMIT = 50
_TALK_READ_LIMIT = 100
_TALK_BODY_CHARS = 4_000
_TALK_MAX_PER_RUN = 20
_TALK_MAX_PER_RECIPIENT = 5
_AGENT_SEARCH_DEFAULT = 10
_AGENT_SEARCH_MAX = 25
_RUN_LIST_DEFAULT = 10
_RUN_LIST_MAX = 25
_AGENT_ANSWER_CHARS = 20_000
_HISTORY_MAX_MATCHES = 12
_HISTORY_MAX_TOTAL_CHARS = 12_000
_HISTORY_SNIPPET_CHARS = 600
_RECALL_MAX_MATCHES = 3
_RECALL_MAX_TOTAL_CHARS = 6_000
_UPDATE_TODOS_MAX = 20
_EXTRACT_MAX_DOCS = 25
_NOTIFY_MAX_PER_RUN = 3
_DECK_MAX_SLIDES = 40
_WORKBOOK_MAX_ROWS = 5_000
_WORKBOOK_MAX_SHEETS = 10
_DOCUMENT_MAX_BLOCKS = 300
_DIAGRAM_MAX_NODES = 24
_DIAGRAM_MAX_EDGES = 40
_WORKBOOK_EDIT_MAX = 200
_SANDBOX_MAX_FILES = 5
_IMAGE_PROMPT_CHARS = 2_000


@dataclass(frozen=True, slots=True)
class Setting:
    """One integer knob. Integers only, deliberately.

    Every knob so far is a budget, and a budget has a floor, a ceiling and a
    default — which is exactly enough to render a control and to validate a
    write without a second schema language. The day a tool needs a string or an
    enum, that is a new field here and a new input in the UI, not a free-form
    JSON editor.
    """

    key: str
    label: str
    help: str
    default: int
    minimum: int
    maximum: int
    unit: str = ''

    def clamp(self, value: int) -> int:
        return max(self.minimum, min(int(value), self.maximum))

    def as_dict(self) -> dict:
        return asdict(self)


#: tool name -> its knobs. Absent from this map means "on/off only", which is
#: true of most of the library and is not a gap.
TOOL_SETTINGS: dict[str, tuple[Setting, ...]] = {
    'web_search': (
        Setting('maxResults', 'Results per search',
                'How many results one search brings back.',
                SEARCH_RESULT_LIMIT, 3, 25),
    ),
    'image_search': (
        Setting('maxResults', 'Images per search',
                'How many images one search brings back.',
                IMAGE_SEARCH_MAX_RESULTS, 1, 12),
    ),
    'video_search': (
        Setting('maxResults', 'Videos per search',
                'How many videos one search brings back.',
                VIDEO_SEARCH_MAX_RESULTS, 1, 10),
    ),
    'read_url': (
        Setting('charLimit', 'Characters per page',
                'How much text is kept from one page. Longer pages are cut.',
                READ_URL_CHAR_LIMIT, 2_000, 60_000, 'characters'),
    ),
    'deep_research': (
        Setting('charLimit', 'Research text budget',
                'Total text kept across every page one research run reads.',
                DEEP_RESEARCH_CHAR_LIMIT, 10_000, 120_000, 'characters'),
        Setting('maxPages', 'Pages to read',
                'Default number of pages a run reads when it does not ask for one.',
                15, 5, 50, 'pages'),
    ),
    # Native Google connectors (`chat/tools/google/`). One knob per shape of
    # result: how many items a listing returns, and how much text a read
    # keeps. Sibling tools share a knob where they return the same thing —
    # `gmail_get_message` reads `gmail_get_thread`'s, `drive_list_recent_files`
    # reads `drive_search_files'` — so one setting does not quietly apply to
    # half of what the user thinks it covers.
    'gmail_search_threads': (
        Setting('maxResults', 'Threads per search',
                'How many email threads one search brings back.',
                10, 1, 50),
    ),
    'gmail_get_thread': (
        Setting('charLimit', 'Email text kept',
                'How much message text one thread or message read keeps.',
                20_000, 2_000, 60_000, 'characters'),
    ),
    'drive_search_files': (
        Setting('maxResults', 'Files per search',
                'How many Drive files one search or recent-files listing returns.',
                15, 1, 50),
    ),
    'drive_read_file_content': (
        Setting('charLimit', 'Characters per read',
                'How much of a Drive file one read returns. Longer files are read in windows.',
                20_000, 2_000, 60_000, 'characters'),
    ),
    'sheets_get_values': (
        Setting('maxRows', 'Rows per read',
                'How many spreadsheet rows one read returns.',
                500, 20, 5_000, 'rows'),
    ),
    'calendar_list_events': (
        Setting('maxResults', 'Events per listing',
                'How many calendar events one listing returns.',
                25, 1, 100),
    ),
    'docs_read': (
        Setting('charLimit', 'Document text kept',
                'How much of a Google Doc one read returns. Longer documents are clipped.',
                20_000, 2_000, 60_000, 'characters'),
    ),
    # Native Notion connector (`chat/tools/notion.py`). Same two shapes:
    # how many items search returns, and reads clip in code with a notice.
    'notion_search': (
        Setting('maxResults', 'Results per search',
                'How many pages and databases one search brings back.',
                10, 1, 50),
    ),
    # 20k mirrors `chat.tools.sandbox.MAX_CODE_OUTPUT_CHARS`, which stays as
    # the floor under a failed overlay read.
    'execute_python': (
        Setting('outputLimit', 'Output kept',
                'How much printed output comes back from one run.',
                20_000, 1_000, 60_000, 'characters'),
    ),
    'scrape_webpage': (
        Setting('charLimit', 'Characters per page',
                'How much text is kept from one scraped page. Longer pages are cut.',
                _SCRAPE_CHAR_LIMIT, 2_000, 60_000, 'characters'),
    ),
    'knowledge_base_search': (
        Setting('topK', 'Results per search',
                'How many chunks one knowledge-base search brings back.',
                _KB_TOP_K, 1, 20),
        Setting('snippetChars', 'Characters per chunk',
                'How much text is kept from each matching chunk.',
                _KB_SNIPPET_CHARS, 500, 8_000, 'characters'),
    ),
    'keyword_search': (
        Setting('topK', 'Results per search',
                'How many chunks one keyword search brings back.',
                _KB_TOP_K, 1, 20),
        Setting('snippetChars', 'Characters per chunk',
                'How much text is kept from each matching chunk.',
                _KB_SNIPPET_CHARS, 500, 8_000, 'characters'),
    ),
    'list_documents': (
        Setting('maxDocs', 'Documents per listing',
                'How many documents one listing returns.',
                _DOC_LIST_CAP, 10, 200),
    ),
    'read_document': (
        Setting('windowChars', 'Characters per read',
                'How much text one document read returns. Longer documents are read in windows.',
                _READ_WINDOW_CHARS, 2_000, 60_000, 'characters'),
    ),
    'list_files': (
        Setting('maxEntries', 'Entries per listing',
                'How many folders and documents one file listing returns.',
                _FILE_LIST_LIMIT, 20, 1_000),
    ),
    'find_files': (
        Setting('maxEntries', 'Files per search',
                'How many files one file search returns.',
                _FILE_LIST_LIMIT, 20, 1_000),
    ),
    'read_file': (
        Setting('windowChars', 'Characters per read',
                'How much text one file read returns. Longer files are read in windows.',
                _FILE_READ_CHARS, 2_000, 60_000, 'characters'),
    ),
    'write_file': (
        Setting('maxChars', 'Characters per write',
                'How much text one file write may store.',
                _FILE_WRITE_CHARS, 10_000, 500_000, 'characters'),
    ),
    'edit_file': (
        Setting('maxChars', 'Characters per edit',
                'How much text one file edit may store.',
                _FILE_WRITE_CHARS, 10_000, 500_000, 'characters'),
    ),
    'query_sql': (
        Setting('maxRows', 'Rows per query',
                'How many rows come back inline. More are saved to a CSV file.',
                _SQL_ROW_CAP, 100, 5_000, 'rows'),
    ),
    'list_api_operations': (
        Setting('maxOps', 'Operations per listing',
                'How many API operations one listing returns.',
                _API_OP_LIST_LIMIT, 5, 100),
    ),
    'call_api': (
        Setting('responseChars', 'Characters per response',
                'How much of an API response comes back inline. Larger responses are saved to a file.',
                _API_RESPONSE_CHARS, 4_000, 64_000, 'characters'),
    ),
    'browse_page': (
        Setting('textChars', 'Characters per page',
                'How much rendered text one browser read keeps.',
                _BROWSER_TEXT_CHARS, 2_000, 60_000, 'characters'),
    ),
    'browser_act': (
        Setting('maxSteps', 'Steps per call',
                'How many browser steps one call may take.',
                _BROWSER_MAX_STEPS, 3, 30, 'steps'),
    ),
    'text_to_speech': (
        Setting('maxChars', 'Characters per call',
                'How much text one call speaks. Split longer passages yourself.',
                _TTS_MAX_CHARS, 1_000, 10_000, 'characters'),
    ),
    'ocr_document': (
        Setting('maxPages', 'Pages per call',
                'How many PDF pages one call re-reads. Fewer is faster.',
                _OCR_MAX_PAGES, 1, 50, 'pages'),
        Setting('pageChars', 'Characters per page',
                'How much text is kept from each re-read page.',
                _OCR_PAGE_CHARS, 2_000, 20_000, 'characters'),
        Setting('maxRows', 'Rows per table read',
                'How many rows a table-mode read returns.',
                _OCR_MAX_ROWS, 20, 1_000, 'rows'),
    ),
    'request_signature': (
        Setting('maxSigners', 'Signers per request',
                'How many signers one signature request may name.',
                _ESIGN_MAX_SIGNERS, 1, 20),
    ),
    'message_search': (
        Setting('maxResults', 'Messages per search',
                'How many messages one messaging search returns.',
                _TALK_SEARCH_LIMIT, 10, 200),
    ),
    'message_read': (
        Setting('maxLimit', 'Messages per read',
                'How many messages one conversation read returns.',
                _TALK_READ_LIMIT, 10, 200),
    ),
    'message_send': (
        Setting('bodyChars', 'Characters per message',
                'How much text one message holds. Split longer messages.',
                _TALK_BODY_CHARS, 500, 10_000, 'characters'),
        Setting('maxPerRun', 'Messages per run',
                'How many messages one run may send in total.',
                _TALK_MAX_PER_RUN, 1, 50),
        Setting('maxPerRecipient', 'Messages per recipient',
                'How many messages one run may send to one recipient.',
                _TALK_MAX_PER_RECIPIENT, 1, 20),
    ),
    'message_draft': (
        Setting('bodyChars', 'Characters per draft',
                'How much text one draft holds.',
                _TALK_BODY_CHARS, 500, 10_000, 'characters'),
    ),
    'search_agents': (
        Setting('maxResults', 'Agents per search',
                'How many agents one search returns.',
                _AGENT_SEARCH_MAX, 5, 50),
    ),
    'list_user_runs': (
        Setting('maxResults', 'Runs per listing',
                'How many of your runs one listing returns.',
                _RUN_LIST_MAX, 5, 50),
    ),
    'search_conversation_history': (
        Setting('maxMatches', 'Messages per search',
                'How many past messages one history search returns.',
                _HISTORY_MAX_MATCHES, 3, 50),
        Setting('totalChars', 'Total characters',
                'Ceiling on the whole history-search result.',
                _HISTORY_MAX_TOTAL_CHARS, 2_000, 60_000, 'characters'),
    ),
    'recall_context': (
        Setting('maxMatches', 'Items per recall',
                'How many archived items one recall returns.',
                _RECALL_MAX_MATCHES, 1, 10),
        Setting('totalChars', 'Total characters',
                'Ceiling on the whole recall result.',
                _RECALL_MAX_TOTAL_CHARS, 1_000, 30_000, 'characters'),
    ),
    'update_todos': (
        Setting('maxItems', 'Steps per plan',
                'How many steps one plan may hold. Track work in fewer, larger steps.',
                _UPDATE_TODOS_MAX, 5, 50),
    ),
    'extract_data': (
        Setting('maxDocs', 'Documents per call',
                'How many documents one extraction call may take.',
                _EXTRACT_MAX_DOCS, 1, 100),
    ),
    'notify_user': (
        Setting('maxPerRun', 'Notifications per run',
                'How many notifications one run may send.',
                _NOTIFY_MAX_PER_RUN, 1, 10),
    ),
    'render_deck': (
        Setting('maxSlides', 'Slides per deck',
                'How many slides one deck may hold.',
                _DECK_MAX_SLIDES, 10, 100),
    ),
    'render_workbook': (
        Setting('maxRows', 'Rows per sheet',
                'How many data rows one sheet may hold.',
                _WORKBOOK_MAX_ROWS, 500, 20_000, 'rows'),
        Setting('maxSheets', 'Sheets per workbook',
                'How many sheets one workbook may hold.',
                _WORKBOOK_MAX_SHEETS, 1, 20),
    ),
    'render_document': (
        Setting('maxBlocks', 'Blocks per document',
                'How many content blocks one document may hold.',
                _DOCUMENT_MAX_BLOCKS, 50, 1_000),
    ),
    'render_pdf': (
        Setting('maxBlocks', 'Blocks per document',
                'How many content blocks one PDF may hold.',
                _DOCUMENT_MAX_BLOCKS, 50, 1_000),
    ),
    'render_diagram': (
        Setting('maxNodes', 'Nodes per diagram',
                'How many boxes one diagram may hold.',
                _DIAGRAM_MAX_NODES, 5, 50),
        Setting('maxEdges', 'Arrows per diagram',
                'How many arrows one diagram may hold.',
                _DIAGRAM_MAX_EDGES, 5, 100),
    ),
    'edit_workbook': (
        Setting('maxEdits', 'Edits per call',
                'How many cell edits one call may make.',
                _WORKBOOK_EDIT_MAX, 20, 1_000),
    ),
    'run_python_on_files': (
        Setting('maxFiles', 'Files per run',
                'How many files one sandbox run accepts in each direction.',
                _SANDBOX_MAX_FILES, 1, 10, 'files'),
    ),
    'generate_image': (
        Setting('promptChars', 'Characters per prompt',
                'How long an image prompt may be.',
                _IMAGE_PROMPT_CHARS, 500, 5_000, 'characters'),
    ),
}

#: Tools whose switch is not the user's to flip.
#:
#: Not a paternalism list — each of these is named by text we put in front of
#: the model. `tool_output.bound` tells it to "call read_tool_output with that
#: id" and the curator's notices name `recall_context`; turning either off
#: makes those instructions dishonest, and an escape hatch nobody can open is
#: worse than none. `get_current_time` reads a clock, has no egress and is what
#: `ALWAYS_AVAILABLE` means.
LOCKED_TOOLS = frozenset({
    'get_current_time',
    'read_tool_output',
    'recall_context',
})


def settings_for(tool_name: str) -> tuple[Setting, ...]:
    return TOOL_SETTINGS.get(tool_name, ())


def defaults_for(tool_name: str) -> dict[str, int]:
    return {s.key: s.default for s in settings_for(tool_name)}


def clean_config(tool_name: str, raw: dict) -> dict[str, int]:
    """Keep only declared keys, coerced to int and clamped to their range.

    Unknown keys are dropped rather than rejected: the caller is a UI that may
    be a deploy behind, and a stale field should not fail a save the user made
    for a different reason. A *value* out of range is clamped for the same
    reason — the control could not have offered it, so the number is noise, not
    an instruction. What is rejected loudly (in the serializer) is an unknown
    *tool*, because that is the mistake that writes a row nothing will read.
    """
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, int] = {}
    for setting in settings_for(tool_name):
        if setting.key not in raw:
            continue
        value = raw[setting.key]
        if isinstance(value, bool) or value is None:
            continue
        try:
            cleaned[setting.key] = setting.clamp(int(value))
        except (TypeError, ValueError):
            continue
    return cleaned
