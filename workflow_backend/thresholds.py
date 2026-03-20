# ==================== Chat Context & Window Limits ====================
MAX_CONTEXT_TOKENS = 100_000  # 100K token hard limit
HISTORY_WINDOW = 50  # Max messages to consider for history
ASSISTANT_SUMMARY_WORD_LIMIT = 300  # Summarize AI responses longer than this
FLASH_SUMMARY_CHAR_LIMIT = 30_000  # Truncate content for summary generation to prevent context bloat

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
