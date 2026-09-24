"""
Web commands: `/search`, `/read`, `/download` (§18.6).

The quick web tier. `/research` is the deep one (multi-source synthesis);
these are the three single moves people actually type: answer from the live
web with citations, read one page, keep one URL as a file. Each pins the
toolbox for one turn so the model reaches for the right tool first.
"""
from __future__ import annotations

import logging

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)


@command(
    name="search",
    summary="Quick web answer with sources — /research is the deep dive",
    args=[Arg("text", kind="text", required=False, hint="What to look up.")],
    kind="turn", group="web",
)
async def search_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    question = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"question": question},
        context_block=(
            "[COMMAND /search]\nAnswer with web_search"
            + (f": {question}" if question else "")
            + ". One search round, then answer with inline sources — this is "
              "the quick lookup, not deep research. Say what you could not verify."
        ),
        # Pinned, not merely suggested: choosing `/search` is the user
        # stating the tool should run, so `_seed_intent_tool` runs it before
        # the model's first turn (the `_INTENT_SEED_TOOL` contract). Without
        # this, a resolved `/search` only narrowed the toolbox while the
        # legacy unregistered path seeded — so the turn searched or not
        # depending on which command modules happened to be imported, i.e. on
        # test order. `/research` already pins its intent for the same reason.
        intent="search",
        tool_pin=("web_search",),
    )


@command(
    name="read",
    summary="Read a page — URL or 'this page'",
    args=[Arg("text", kind="text", required=False, hint="The URL to read.")],
    kind="turn", group="web",
)
async def read_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    target = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"target": target},
        context_block=(
            "[COMMAND /read]\nRead the page with scrape_webpage (read_url for "
            "a raw fetch)"
            + (f": {target}" if target else "")
            + ". Summarise what it says, quote the load-bearing lines, link "
              "the source. If it refuses (paywall, login, bot check), say so "
              "rather than answering from memory."
        ),
        tool_pin=("scrape_webpage", "read_url"),
    )


@command(
    name="download",
    summary="Keep a URL as my file",
    args=[Arg("text", kind="text", required=False, hint="The URL to keep.")],
    kind="turn", group="web",
)
async def download_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    target = str(call.args.get("text") or call.text or "").strip()
    if not target:
        return CommandResult(
            status="error", message="Give me the URL to keep — /download <url>."
        )
    return CommandResult(
        status="ok", args={"url": target},
        context_block=(
            f"[COMMAND /download]\nFetch {target} with download_file into the "
            f"caller's write folder and reply with the saved path. Internal "
            f"hosts are refused — say so if that is what the URL is."
        ),
        tool_pin=("download_file",),
    )
