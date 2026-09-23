"""
Tier-3 commands: shortcuts to pages that exist or are being built (§18.6).

`/code <project>` (open the Code tab on a project), `/browse <url>` (a
browser-pinned turn with the live view open), `/sql <connection> <question>`
(and `/api` the same shape for HTTP APIs), `/connect <service>` (open that
Connections card), `/approvals` (open the Inbox), `/publish` (publish the
last artifact through the page visibility sheet), `/message` (reach a
channel), `/sign` (send a document for signature), `/run` (Python, computed
not guessed).

Client commands open something; turn commands pin the toolbox for one turn.
None invents a capability — each reaches something the platform already has.
Outward turns (`/message`, `/sign`) carry no confirm sheet: the turn itself
is side-effect free, and the send still gates at dispatch through the usual
approval card, with the model having prepared it.
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
    name="api",
    summary="Call one of my HTTP APIs",
    args=[
        Arg("connection", kind="connection", required=False,
            hint="Which API."),
        Arg("text", kind="text", required=False, hint="What to do."),
    ],
    kind="turn", group="data",
)
async def api_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    connection = call.args.get("connection")
    goal = str(call.args.get("text") or call.text or "").strip()
    if connection is None and not goal:
        return CommandResult(status="ok", card={"type": "api_pick"})
    context_block = (
        "[COMMAND /api]\nReach the user's HTTP APIs with "
        "list_api_operations and call_api"
        + (f" (connection {connection})" if connection is not None else "")
        + (f". Goal: {goal}" if goal else ".")
        + " Reads first — a call with side effects pauses for approval, and "
          "secret values travel as references, never inline."
    )
    return CommandResult(
        status="ok",
        args={**({"connection": connection} if connection is not None else {}),
              "goal": goal},
        context_block=context_block,
        tool_pin=("list_api_operations", "call_api"),
    )


@command(
    name="message",
    summary="Reach a channel — draft, then send on approval",
    args=[Arg("text", kind="text", required=False, hint="Who and what.")],
    kind="turn", group="talk",
)
async def message_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    text = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"text": text},
        context_block=(
            "[COMMAND /message]\nReach the user's messaging channels with "
            "message_channels, message_search, message_read, message_draft "
            "and message_send"
            + (f": {text}" if text else "")
            + ". Draft first and read it back; the send itself pauses for "
              "approval — an unseen recipient list is never a guess."
        ),
        tool_pin=("message_channels", "message_search", "message_read",
                  "message_draft", "message_send"),
    )


@command(
    name="sign",
    summary="Send a document out for signature",
    args=[Arg("text", kind="text", required=False, hint="Which document, to whom.")],
    kind="turn", group="office", requires="esign",
)
async def sign_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    text = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"text": text},
        context_block=(
            "[COMMAND /sign]\nSend a document for e-signature with "
            "request_signature (track it with signature_status)"
            + (f": {text}" if text else "")
            + ". Outward-facing: confirm the document and the signer aloud "
              "before calling, and the send pauses for approval."
        ),
        tool_pin=("request_signature", "signature_status"),
    )


@command(
    name="run",
    summary="Run Python for this — compute, don't guess",
    args=[Arg("text", kind="text", required=False, hint="What to compute.")],
    kind="turn", group="code",
)
async def run_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    task = str(call.args.get("text") or call.text or "").strip()
    return CommandResult(
        status="ok", args={"task": task},
        context_block=(
            "[COMMAND /run]\nCompute with execute_python in the sandbox"
            + (f": {task}" if task else "")
            + ". Numbers a turn needs come from running code, not from "
              "memory — and the answer quotes the result, not the script."
        ),
        tool_pin=("execute_python",),
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
