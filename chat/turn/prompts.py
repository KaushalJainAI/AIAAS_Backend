"""
Prompt construction for the chat agent.

Deliberately small. The tool *schemas* are passed to the model natively via the
`tools` parameter, so this file does not re-list them in prose — a second copy in
the system prompt drifts from the real list and, worse, keeps advertising tools
on the final iteration when they are withheld to force an answer.

There is also no output-format contract here. The agent streams plain markdown;
follow-up questions are a separate structured call (see `FOLLOW_UPS_*`). That is
what lets the answer stream token-by-token instead of arriving as one JSON blob.

## Baseline vs. volatile

Two builders, split on how often the text changes:

`build_system_message` returns the **baseline** — the session's own prompt, the
core rules, and the memory rule. It changes only when the user edits the session
prompt or flips memory, so it is byte-identical across the turns of a normal
conversation and every provider that caches request prefixes can reuse it.

`build_context_update` returns the **volatile** facts: the wall clock, the mode
nudge for this turn's detected intent, and any attachments withheld from this
model. These used to be concatenated into the system message, which made the
prefix differ on every single turn — the clock alone guaranteed a cache miss.
The caller
appends the result to history as a trailing `system` message instead, the same
shape `llm.clamp_input` already uses for its trim notice. It lands after the
prior conversation and before the user's prompt, so it reads as "here is the
state right now" and leaves everything ahead of it stable.
"""
from workflow_backend.thresholds import HISTORY_WINDOW


CORE_RULES = """
### CORE OPERATING RULES ###
1. GROUNDING: Never invent facts, dates, figures or URLs. For anything current —
   news, prices, releases, "latest" — search before answering. If you cannot
   verify something, say so.
2. CITATIONS: Base claims on tool output when you have it, and cite the source
   inline as a markdown link.
3. TOOL ECONOMY: Tools cost the user time. Answer directly from your own
   knowledge when that is genuinely sufficient. When you do call a tool, use the
   result — do not re-run the same call hoping for a better answer.
4. RESILIENCE: If a tool fails or returns too little, try a different query or
   source before giving up. Report what you could not find rather than guessing.
5. SHOWING vs TELLING: When the answer is a chart, diagram, comparison table or
   small interactive demo, call `render_html_artifact` with self-contained HTML
   rather than describing it in prose. Inline all CSS/JS — the sandbox iframe
   blocks external requests.
6. DOCUMENTS: Older turns are shown to you as summaries. To quote or analyse a
   file or page in detail, call `read_attachment_text` or `read_url` for the full
   text instead of asking the user to upload it again.
7. BORROWED SIGHT: If you are told an image is attached that you cannot see,
   call `ask_vision` with its id rather than apologising or guessing. What comes
   back is testimony from another model, not something you saw — so never write
   "I can see that..."; write "the image shows..." or attribute it plainly. Ask a
   follow-up instead of filling a gap by inference, and if the witness hedges or
   flags a reading as uncertain, pass that uncertainty on to the user rather than
   laundering it into a clean number.
8. FORMAT: Answer in clean markdown. Use language-tagged code fences for code.
"""

MEMORY_ON_RULE = f"""
9. RECALL: You can see only the last {HISTORY_WINDOW} turns. The rest of this
   conversation is stored and searchable — it is not lost. If the user refers to
   anything outside your window, call `search_conversation_history` before
   answering. Replying "I don't have that in my context" without searching first
   is a failure.
"""

MEMORY_OFF_RULE = """
9. NO MEMORY THIS TURN: The user has switched memory off, so you can see only
   their current message. If they refer to something discussed earlier, say
   plainly that memory is off and ask them to restate it. Do not pretend to
   recall it.
"""

MODE_RULES = {
    'research': (
        "\n### DEEP RESEARCH MODE ###\n"
        "Call `deep_research` first — it plans queries, searches and reads the "
        "pages in one step. Synthesise across sources, surface disagreements "
        "between them, and cite each claim."
    ),
    'image': (
        "\n### IMAGE MODE ###\n"
        "The user wants visuals. Prefer `image_search`."
    ),
    'video': (
        "\n### VIDEO MODE ###\n"
        "The user wants video. Prefer `video_search`."
    ),
}


def build_system_message(session) -> str:
    """
    Assemble the stable baseline system prompt for a session.

    Nothing here may vary turn to turn. Anything that does belongs in
    `build_context_update`, or it costs every session its cached prefix.
    """
    base = session.system_prompt or (
        "You are a helpful, knowledgeable AI assistant. Be concise but thorough."
    )

    parts = [
        base,
        "\n### CONTEXT ###"
        "\n- Your training data is stale; assume you do not know recent events."
        "\n- The current date and time, anything on the user's screen, and any"
        " files withheld from you are reported separately as they change; trust"
        " the most recent such report over anything earlier in this conversation.",
        CORE_RULES,
        MEMORY_ON_RULE if session.memory_enabled else MEMORY_OFF_RULE,
    ]
    return "\n".join(p for p in parts if p)


def build_context_update(
    session, current_time: str, intent: str, *, blocked_notice: str = ""
) -> str:
    """
    Render the facts that change from turn to turn, or "" if there are none.

    The caller appends this to history as a trailing `system` message rather
    than folding it into the baseline - see the module docstring. `intent` is
    included because it is re-detected per message: a mode nudge is a fact about
    *this* turn, not a standing instruction.
    """
    parts = [
        f"### CURRENT STATE ###\n- Current date/time: {current_time}",
        MODE_RULES.get(intent, ""),
        blocked_notice,
    ]
    return "\n".join(part for part in parts if part).strip()


# ── Continuation nudges ──────────────────────────────────────────────────────
# Appended as the trailing user message when the transcript ends on tool output.
# The LLM handlers always append `prompt` as the final user turn, so a tool
# result can never be last on the wire; this makes that trailing turn carry
# something useful instead of a filler token.

CONTINUE = (
    "Continue from the tool results above. Call another tool only if you still "
    "need information; otherwise give your final answer to the user."
)

CONTINUE_AT_LIMIT = (
    "You have reached the tool-call limit for this turn. Answer now using what "
    "you already have, and say plainly what you could not verify. Do not call "
    "any more tools."
)


# ── Follow-up questions ──────────────────────────────────────────────────────
# A small, cheap second call rather than a field the main answer must carry.
# Asking the model to wrap a long markdown answer in JSON just to attach three
# questions is what made the answer unstreamable in the first place.

FOLLOW_UPS_SYSTEM = "You generate follow-up questions. Output only JSON."

FOLLOW_UPS_TEMPLATE = """The user asked:
{question}

The assistant answered:
{answer}

Suggest exactly 3 short follow-up questions the user would plausibly ask next.
They must be answerable from this conversation's subject matter and must not
repeat what was already covered.

Respond with only: {{"follow_ups": ["...", "...", "..."]}}"""
