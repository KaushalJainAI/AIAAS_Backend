"""
Tier-3 commands: shortcuts to pages that exist or are being built (§18.6).

`/code <project>` (open the Code tab on a project), `/browse <url>` (a
browser-pinned turn with the live view open), `/sql <connection> <question>`,
`/connect <service>` (open that Connections card), `/approvals` (open the
Inbox), `/publish` (publish the last artifact through the page visibility
sheet).

Client commands open something; turn commands pin the toolbox for one turn.
None invents a capability — each reaches something the platform already has.
"""
from __future__ import annotations

import logging

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)


@command(
    name="code",
    summary="Open the Code tab on a project",
    args=[Arg("project", kind="project", required=False, hint="Which project.")],
    kind="client", group="code", requires="workspace",
)
async def code_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    project = call.args.get("project")
    return CommandResult(
        status="ok",
        args={"project": project} if project else {},
        card={"type": "code", **({"project": project} if project else {})},
    )


@command(
    name="browse",
    summary="A browser-pinned turn with the live view open",
    args=[Arg("text", kind="text", required=False, hint="URL or goal.")],
    kind="turn", group="browse", requires="browser",
)
async def browse_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    target = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"target": target},
        context_block=(
            "[COMMAND /browse]\nThis is a browser-pinned turn"
            + (f" on {target}" if target else "")
            + ". Use browse_page to read and browser_act to act; narrate what "
              "each step did, and pause (ask) before any submit step."
        ),
        tool_pin=("browse_page", "browser_act"),
        card={"type": "browse_live"} if target else {"type": "browse"},
    )


@command(
    name="sql",
    summary="Ask a question of a database connection",
    args=[
        Arg("connection", kind="connection", required=False,
            hint="Which database."),
        Arg("text", kind="text", required=False, hint="The question."),
    ],
    kind="turn", group="data",
)
async def sql_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    connection = call.args.get("connection")
    question = str(call.args.get("text") or call.text or "").strip()
    if connection is None and not question:
        return CommandResult(status="ok", card={"type": "sql_pick"})
    context_block = (
        "[COMMAND /sql]\nAnswer from the user's databases with "
        "list_data_connections, describe_schema and query_sql"
        + (f" (connection {connection})" if connection is not None else "")
        + (f". Question: {question}" if question else ".")
        + " Reads only — execute_sql only where the owner allowed it, and "
          "then with approval."
    )
    return CommandResult(
        status="ok",
        args={**({"connection": connection} if connection is not None else {}),
              "question": question},
        context_block=context_block,
        tool_pin=("list_data_connections", "describe_schema", "query_sql"),
    )


@command(
    name="connect",
    summary="Open a Connections card",
    args=[Arg("connection", kind="connection", required=False,
              hint="Which service.")],
    kind="client", group="general",
)
async def connect_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    connection = call.args.get("connection")
    return CommandResult(
        status="ok",
        args={"connection": connection} if connection is not None else {},
        card={"type": "connect",
              **({"connection": connection} if connection is not None else {})},
    )


@command(
    name="approvals",
    summary="Open the Inbox",
    kind="client", group="general",
)
async def approvals_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    return CommandResult(status="ok", card={"type": "approvals"})


@command(
    name="publish",
    summary="Publish the last artifact through the visibility sheet",
    args=[Arg("visibility", kind="visibility", required=False,
              hint="link, platform or public.")],
    kind="turn", group="office",
)
async def publish_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    visibility = str(call.args.get("visibility") or "link").strip().lower()
    if visibility not in ("link", "platform", "public"):
        return CommandResult(
            status="error", message="Visibility is link, platform or public."
        )
    return CommandResult(
        status="confirm",
        message="Publishing leaves the platform. Review before it goes out.",
        args={"visibility": visibility},
        card={"type": "publish_confirm", "visibility": visibility},
        context_block=(
            f"[COMMAND /publish {visibility}]\nPublish the conversation's last "
            f"artifact (dashboard, deck, document or page) with publish_page at "
            f"visibility {visibility}. Default to the narrowest that works — "
            f"sharing with one colleague is not publishing to the open internet."
        ),
        tool_pin=("publish_page",),
    )
