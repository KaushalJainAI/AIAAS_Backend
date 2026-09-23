"""
Knowledge commands: `/kb`, `/extract` (§18.6).

The RAG tools had no command spelling them, so a corpus the user built was
reachable only by describing it well enough for the model to guess the tool.
`/kb` answers from the user's documents (scoped like every other KB read —
the agent-scope rules in `chat/tools` apply unchanged); `/extract` pulls
structured rows through the extraction engine instead of prose.
"""
from __future__ import annotations

import logging

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)

_RAG_PIN = (
    "list_knowledge_bases",
    "knowledge_base_search",
    "keyword_search",
    "list_documents",
    "read_document",
)


@command(
    name="kb",
    summary="Answer from my documents",
    args=[Arg("text", kind="text", required=False, hint="The question.")],
    kind="turn", group="library",
)
async def kb_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    question = str(call.args.get("text") or call.text or "").strip()
    if not question:
        return CommandResult(
            status="error", message="Ask about your documents — /kb <question>."
        )
    return CommandResult(
        status="ok", args={"question": question},
        context_block=(
            f"[COMMAND /kb]\nAnswer from the user's knowledge bases: "
            f"list_knowledge_bases, then the search that fits the index "
            f"(knowledge_base_search on semantic, keyword_search on keyword, "
            f"list_documents + read_document on raw). Cite which document "
            f"each claim came from; say plainly when nothing matches.\n"
            f"QUESTION: {question}"
        ),
        tool_pin=_RAG_PIN,
    )


@command(
    name="extract",
    summary="Pull structured rows via an extraction schema",
    args=[Arg("text", kind="text", required=False, hint="What to pull out.")],
    kind="turn", group="library",
)
async def extract_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    target = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"target": target},
        context_block=(
            "[COMMAND /extract]\nPull structured data with extract_data "
            "through a schema (confidence and the review queue come with "
            "it)"
            + (f": {target}" if target else "")
            + ". Prose is not an extraction — rows, with the schema named."
        ),
        tool_pin=("extract_data",),
    )
