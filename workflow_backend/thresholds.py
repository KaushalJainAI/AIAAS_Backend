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

# ==================== Chart Limits ====================
# `render_chart` takes data and a spec, never markup, so these bound the *data*
# rather than a payload size. The series cap is the one with a reason beyond
# cost: a categorical palette is a fixed, validated order of eight hues, and a
# ninth series would have to invent a colour that no longer separates under
# colour-vision deficiency. Past eight the honest answers are "Other", or two
# charts.
CHART_MAX_SERIES = 8
#: Scatter and bubble compare every pair of series at once rather than adjacent
#: ones, and the validated palette only clears that stricter bar for its first
#: three slots.
CHART_MAX_SERIES_ALL_PAIRS = 3
CHART_MAX_POINTS_PER_SERIES = 200
#: Across every series. A chart with more marks than the viewport has pixels is
#: not a chart anyone reads; it is a table that has been made harder to use.
CHART_MAX_POINTS_TOTAL = 1_000
CHART_MAX_LABEL_CHARS = 80

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
#: Cosine floor a chunk must clear to be returned at all.
#:
#: Retuned 0.30 → 0.15 on 2026-09-01, when the embedder moved to
#: `nvidia/nemotron-3-embed-1b` (the previous model reached end of life). The
#: old value was doubly stale: it was calibrated for a different model, and it
#: was calibrated against scores `engine.py` computed *wrongly* — it squared an
#: already-squared FAISS L2 distance, so every cosine it compared against this
#: floor was understated.
#:
#: Measured against a real indexed chunk with the current model:
#:
#:     relevant queries    0.281 – 0.496   ("what was revenue" is the 0.281)
#:     irrelevant queries  0.002 – 0.021   ("how do I bake sourdough bread")
#:
#: The two bands are more than an order of magnitude apart, and 0.30 sat
#: *inside* the relevant one — a genuinely on-topic question returned nothing.
#: 0.15 is ~7× the loudest irrelevant match and well under the quietest
#: relevant one, so it separates the bands instead of splitting one.
#:
#: Re-measure this when the embedding model changes. Absolute cosine ranges are
#: a property of the model, not of the corpus, and a floor carried across a
#: model swap is exactly how retrieval goes quiet without erroring.
SEARCH_MIN_SCORE = 0.15

# ==================== File System Limits ====================
# Shape caps for the per-user document tree (inference/filesystem.py). These
# bound work, not policy — how long `Folder.path` can get, how much one listing
# may return, how many rows one move may touch. Retention policy lives in
# settings (RECYCLE_BIN_RETENTION_DAYS) because it is deployment-tunable.
#: Deepest a folder may nest. Bounds `Folder.path` length (ids + separators) and
#: the cost of resolving breadcrumbs.
MAX_FOLDER_DEPTH = 20
#: Per user, across the whole tree. A tree is for organising, not for storing.
MAX_FOLDERS_PER_USER = 2_000
#: Children returned by one folder listing. Capped rather than cursored: folder
#: rows are tiny, and a second pagination scheme on one page is worse than a
#: cap. A capped response says `truncated` in its own body.
FOLDER_CHILDREN_LIMIT = 500
#: Ids accepted by one move or restore call.
MAX_MOVE_BATCH = 200

# Caps on what one agent run may do to that tree through the virtual filesystem
# (inference/vfs.py). Separate from the shape caps above because they bound a
# *run's* blast radius rather than the tree's shape: a human clicking "new
# folder" is rate-limited by being a human, and an agent in a loop is not.
#: Characters one `read_file` call returns. Paging is by explicit offset, and a
#: truncated read says so and names the offset to resume from — a silent trim
#: would let a model summarise half a document as if it were the whole one.
AGENT_FILE_READ_CHARS = 30_000
#: Characters one `write_file` call may store. Well under DOCUMENT_EXTRACT_CAP:
#: a model emitting 200k characters has lost the plot, and the cheap failure is
#: the refusal.
AGENT_FILE_WRITE_CHARS = 200_000
#: Entries (folders + documents) one `list_files` call returns.
AGENT_FILE_LIST_LIMIT = 200
#: Root-level folder under which `fileAccess='scoped'` agents get their homes.
#: A real name rather than a hidden one: these files belong to the user, and a
#: tree they cannot see in the UI is a tree they cannot clean up.
AGENT_HOME_ROOT = 'Agents'
#: Root-level folder chat writes into. A sibling of `AGENT_HOME_ROOT` and not a
#: folder underneath it, because chat is not an agent — putting its output in
#: `/Agents/Chat/` would make the one tree the user browses lie about where
#: their files came from.
CHAT_HOME_ROOT = 'Chat'

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

# ==================== Tool Output Limits ====================
# A backstop, not a budget. Every tool that knows what its own result costs sets
# its own ceiling above (deep research 60k, read_url 15k, history search 12k,
# sandbox stdout 20k) and those stay authoritative. This catches the ones that
# never set one — above all MCP tools, whose responses come from a third-party
# server and are bounded by nothing on our side. It sits above the largest
# deliberate budget so a tool that spent its own allowance is never trimmed
# twice; anything reaching it did so because nobody was counting.
TOOL_OUTPUT_CHAR_LIMIT = 64_000
#: Kept in the bounded preview handed back to the model, split head/tail.
TOOL_OUTPUT_PREVIEW_CHARS = 6_000
#: How long the spilled full text stays readable. The bounded preview in the
#: transcript is the durable record; this is only the window in which the model
#: can still go and fetch the rest.
TOOL_OUTPUT_RETENTION_HOURS = 24

# ==================== Context Curation ====================
# What keeps a long agent run inside its window. `clamp_input` is the byte guard
# of last resort and drops whole segments blindly; curation is the deliberate
# version that runs earlier, keeps a record of what it cut, and can be asked for
# it back. See `chat/turn/curation.py`.
#
# Two marks, not one. Curating a little on every turn would rewrite the request
# prefix on every single call and destroy provider prefix caching — the same
# trap the clock-in-the-system-prompt fix documents. So nothing happens until
# the high mark, and then enough is cut to reach the low mark in one pass; in
# between, the prefix is byte-identical from turn to turn.
CONTEXT_HIGH_WATER_RATIO = 0.70   # Of the input budget: start curating
CONTEXT_LOW_WATER_RATIO = 0.45    # Of the input budget: cut back to here
#: Never touched, whatever the ratios say. The model has to be able to see what
#: it just did, or it repeats it.
CONTEXT_KEEP_RECENT_SEGMENTS = 3
#: How much of a collapsed tool result survives in the transcript. Enough to
#: recognise what the result was, never enough to work from — working from it is
#: what `recall_context` and `read_tool_output` are for.
CONTEXT_TOOL_RECORD_CHARS = 400
#: Target length of a recursive summary. Small on purpose: a summary that grows
#: with the run is just the run again.
CONTEXT_SUMMARY_TARGET_WORDS = 250
#: Ceiling on what is fed to the summariser in one pass.
CONTEXT_SUMMARY_INPUT_CHARS = 48_000

# ==================== Context Recall ====================
# `recall_context` reads back what curation archived. Capped harder than the
# window it protects, for the same reason `search_conversation_history` is.
RECALL_MAX_MATCHES = 3
RECALL_SNIPPET_CHARS = 1_500
RECALL_MAX_TOTAL_CHARS = 6_000
RECALL_SCAN_LIMIT = 200  # Newest archived rows examined per query

# ==================== Response Size Limits ====================
# DRF's DEFAULT_PAGINATION_CLASS only reaches generic views and viewsets, and
# most of this API is `@api_view` functions — so the global PAGE_SIZE never
# applied to them and several list responses were bounded by nothing but how
# much work a user happened to do. A run with thousands of node logs, each
# carrying its own input/output JSON, is a response large enough to matter on a
# small box. These are ceilings on a single response, not page sizes: callers
# that need everything should page, and a truncated response says so.
EXECUTION_NODE_LOG_LIMIT = 500   # Node logs returned per execution detail call
EXECUTION_STREAM_LOG_LIMIT = 200  # Node logs replayed when a socket connects
#: Turns returned per execution detail call. A turn's reasoning is stored in
#: full (up to `TURN_REASONING_CHAR_LIMIT` each), so the steps cap alone did
#: not bound the response: a run with thousands of turns would still ship
#: megabytes of reasoning. Capped and flagged like the steps, so a trimmed run
#: and a genuinely short one are distinguishable.
EXECUTION_TURN_LIMIT = 200

# ==================== Agent Observability ====================
# A turn's reasoning is the single most useful thing in a run when debugging why
# an agent did something, so it is stored in full rather than sampled — the old
# write path kept `thinking[-150:]`, which was too short to be evidence of
# anything. It still needs a ceiling: extended-thinking models can emit tens of
# thousands of characters per turn, and a 40-iteration research run would
# otherwise write megabytes per execution. Anything trimmed is *marked*
# (`reasoning_truncated` / `content_truncated`), because a cut thought and a
# genuinely brief one must not look alike.
TURN_REASONING_CHAR_LIMIT = 8_000
TURN_CONTENT_CHAR_LIMIT = 4_000

# Each side of a single field's diff in a SubAgentRevision. An agent's `brief`
# is free text with no length limit of its own, so an edit to it would otherwise
# store the whole prompt twice per revision, for ever.
REVISION_VALUE_CHAR_LIMIT = 4_000

# How many revisions a timeline returns. Revisions are only written on a real
# change, but a heavily-tuned agent accumulates them without limit, and each row
# carries a full diff — so the timeline is capped like `EXECUTION_NODE_LOG_LIMIT`
# and says so in its own body (`truncated`) rather than silently returning
# whichever suffix of history the user happened to ask for.
REVISION_TIMELINE_LIMIT = 200

# ==================== Agent Spend ====================
# What `SubAgent.guardrails['spendCapRupees']` is measured against.
#
# The cap used to be compared against `ExecutionLog.credits_used`, a column
# nothing has ever written — so the guardrail was a no-op and an agent could
# run without limit however low the cap was set. `tokens_used` is the only
# usage number a run actually records, so the cap is denominated against that
# through one rate, applied in exactly one place (`agents.spend.rupees_for`).
#
# Deliberately a single blended rate rather than a per-model price table: the
# cap is a blast-radius control, not billing. It has to be roughly right and
# impossible to bypass, and a table that has to be kept current per model would
# be neither. Raise it if the agents here run on dearer models than the
# mid-tier default this assumes.
RUPEES_PER_MILLION_TOKENS = 85

# ==================== Evaluation ====================
# A sweep runs the agent once per case, so every number here is a bound on how
# much one button press can cost. Concurrency is capped separately from the
# per-suite setting because a suite is user-authored: `suite.concurrency` is a
# preference, this is the ceiling it is clamped to.
EVAL_MAX_CONCURRENCY = 4
EVAL_MAX_CASES_PER_SUITE = 200
# Stored per result. The full answer stays reachable through
# `EvalResult.execution` (the run's own output_data), so this only bounds the
# copy the results table carries — and `answer_truncated` marks when it was cut,
# because a trimmed answer and a terse one must not look alike.
EVAL_RESULT_ANSWER_CHAR_LIMIT = 16_000
# Ceilings on a single list response, in the same spirit as
# EXECUTION_NODE_LOG_LIMIT: these are `@api_view` functions, which DRF's
# DEFAULT_PAGINATION_CLASS never reaches, so the cap is ours to set and the
# body says when it applied.
EVAL_RUN_LIST_LIMIT = 100
EVAL_RESULT_LIST_LIMIT = 200
EVAL_REVIEW_QUEUE_LIMIT = 100

# ==================== Agent Time ====================
# The resource an agent run actually contends for is *wall-clock time*, not CPU
# and not memory. A run spends almost all of its life awaiting a provider, and
# while it does so it holds an event-loop slot, a checkpointer thread, a DB
# connection and a socket — none of which a CPU or memory number could bound,
# and none of which this deployment could enforce anyway (the sandbox is `exec`
# on a worker thread inside the Django process; there is no cgroup to hang a
# quota off). So the knob is seconds, and unlike the two it replaces it is read.
#
# Denominated in seconds throughout and shown in minutes, for the same reason
# the spend cap is stored in rupees: the stored unit should be the one the
# runtime compares against, so no reader has to remember a conversion.
DEFAULT_RUN_SECONDS = 15 * 60
MIN_RUN_SECONDS = 30
#: The ceiling a user may set. Above this a run is not "long", it is stuck, and
#: the honest failure is the one that returns partial work while someone is
#: still watching for it.
MAX_RUN_SECONDS = 2 * 60 * 60

#: Left for the run to write its answer once the clock is nearly out. The soft
#: stop fires here rather than at zero: a run killed mid-tool-call has paid for
#: everything and returns nothing, while one that stops *asking* for tools with
#: this much left gets to summarise what it found. Withholding tools is exactly
#: what `max_iterations` already does on its last pass, so the mechanism is
#: shared rather than invented.
RUN_WRAPUP_SECONDS = 45

#: Of the parent's *remaining* time, the share one fan-out may consume. Time is
#: not divided across workers the way money is — concurrent workers do not add
#: up in wall-clock the way their spend does — so a worker is bounded by a
#: shared deadline instead. What the reserve buys is the parent's own last turn:
#: a worker allowed to run until the parent's clock hits zero returns eight
#: answers into a run with no time left to read them, which is the one outcome
#: worse than delegating nothing.
WORKER_DEADLINE_SHARE = 0.8
#: Below this there is no point starting a worker at all, so the delegation is
#: refused with a message the model can act on rather than N runs that each die
#: on their first model call.
MIN_WORKER_SECONDS = 60

# ==================== Agent Admission ====================
# What stops one user's runs from being every run on the box. Per-process, so
# these bound one ASGI worker rather than the cluster — which is the shape of
# this deployment (a single box) and is stated rather than implied, because a
# limit that silently means something else at two processes is worse than none.
#
# Only *top-level* runs are gated. A delegated worker must never queue behind
# its own parent: the parent holds a slot while awaiting the worker, so gating
# the worker on the same semaphore is a deadlock, not a limit.
MAX_CONCURRENT_RUNS_PER_USER = 3
MAX_CONCURRENT_RUNS_TOTAL = 12
#: How long a run waits for a slot before it is refused. Bounded because the
#: caller already has a 202 and an execution id: a run that waits for ever is
#: indistinguishable, from the outside, from one that is running.
ADMISSION_WAIT_SECONDS = 120
