# ==================== Chat Context & Window Limits ====================
MAX_CONTEXT_TOKENS = 100_000  # 100K token hard limit
# How many *conversational* turns (user + assistant only, never system) get
# replayed verbatim. Everything older stays in the DB and is reachable through
# the search_conversation_history tool, so shrinking this window loses no data —
# it only stops us paying for context the model usually does not need.
HISTORY_WINDOW = 20
ASSISTANT_SUMMARY_WORD_LIMIT = 300  # Summarize AI responses longer than this
FLASH_SUMMARY_CHAR_LIMIT = 30_000  # Truncate content for summary generation to prevent context bloat

# The last line of defence before a request leaves for the provider. The
# per-section budgets above are advisory and each one is computed in isolation,
# so a turn that hits several of them at once can still add up to more than the
# model accepts. clamp_llm_input applies this to the assembled payload, which is
# the only place the true total is known.
MAX_LLM_INPUT_TOKENS = 96_000
MAX_SINGLE_MESSAGE_TOKENS = 24_000  # One message may not eat the whole budget

# ==================== Conversation History Retrieval ====================
# Bounds for the grep-style lookup into older turns. The point of the tool is to
# *avoid* blowing the window, so its own output has to be capped harder than the
# window it is protecting.
HISTORY_SEARCH_MAX_MATCHES = 12       # Messages returned per search
HISTORY_SEARCH_SNIPPET_CHARS = 600    # Characters of context around each hit
HISTORY_SEARCH_MAX_TOTAL_CHARS = 12_000  # Hard ceiling on the whole tool result
HISTORY_SEARCH_MAX_PATTERN_LEN = 200  # Guards against pathological queries
# Newest-N messages the search will scan. A cap is needed because the scan runs
# in Python (see _search_conversation_history for why not the DB), so cost is
# linear in messages examined.
HISTORY_SEARCH_SCAN_LIMIT = 800

# ==================== HTML Artifact Limits ====================
# The model can author markup, so every number here is a containment boundary
# rather than a style preference: an artifact must not be able to cover the page
# or push the composer off screen.
HTML_ARTIFACT_MAX_CHARS = 24_000
HTML_ARTIFACT_MAX_WIDTH = 720   # px
HTML_ARTIFACT_MAX_HEIGHT = 520  # px
HTML_ARTIFACT_MIN_WIDTH = 160   # px
HTML_ARTIFACT_MIN_HEIGHT = 120  # px
HTML_ARTIFACT_DEFAULT_WIDTH = 640   # px
HTML_ARTIFACT_DEFAULT_HEIGHT = 360  # px

# ==================== File & Document Limits ====================
IS_LARGE_FILE_THRESHOLD = 120_000  # Characters before a file is considered "large" (triggers RAG instead of direct injection)
LARGE_FILE_PREVIEW_LENGTH = 120_000  # Characters of preview to inject into context
DOCUMENT_EXTRACT_CAP = 500_000  # Maximum characters to extract from uploaded files

# ==================== Search & Tool Limits ====================
SEARCH_RESULT_LIMIT = 15  # Number of web search results per query
WEB_SEARCH_MAX_RETRIES = 5
IMAGE_SEARCH_MAX_RESULTS = 6
VIDEO_SEARCH_MAX_RESULTS = 4
MAX_TOOL_ITERATIONS = 12

# ==================== Deep Research Limits ====================
DEEP_RESEARCH_LINK_MIN = 20
DEEP_RESEARCH_LINK_MAX = 100
DEEP_RESEARCH_CHAR_LIMIT = 60_000  # Char limit for combined deep research text
URL_SCRAPE_CHAR_LIMIT = 4_000  # Char limit per deeply scraped URL
READ_URL_CHAR_LIMIT = 15_000  # Regular read_url tool character limit

# ==================== RAG & Inference Limits ====================
CHUNK_SIZE = 500
CHUNK_OVERLAP = 50
SEARCH_TOP_K = 5
SEARCH_MIN_SCORE = 0.3

# ==================== Execution & Workflow Limits ====================
DEFAULT_HITL_TIMEOUT_SECONDS = 300  # Human-in-the-loop timeout
MAX_LOOP_COUNT = 1000  # Max workflow execution loops
EXECUTION_TTL_SECONDS = 3600  # 1 hour execution data TTL

# ==================== Upload & Request Limits ====================
MAX_DOCUMENT_SIZE = 50 * 1024 * 1024  # 50MB limit for general uploads
DATA_UPLOAD_MAX_MEMORY_SIZE = 104857600  # 100MB for request payload memory
FILE_UPLOAD_MAX_MEMORY_SIZE = 104857600  # 100MB for file upload memory
DATA_UPLOAD_MAX_NUMBER_FIELDS = 10000    # Increase field limit for complex workflows

# ==================== Subprocess & Internal Timeouts ====================
IMPORT_CHECK_TIMEOUT_SECONDS = 15  # Import checking timeout
