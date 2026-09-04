"""
Which tool call needs a human, decided per call rather than per name.

`SENSITIVE_TOOLS` is a list of built-in tool *names*, and that was enough while
every tool that could act on the user's behalf was one we wrote. MCP tools are
not. Their names are minted at runtime from a third-party server's catalogue
(`mcp__<server_id>__<tool>_<digest>`), so no static list can contain them — and
`mcp_integration.credential_injector` hands them the user's real credentials at
invocation time. The result was that `mcp__7__send_email_ab12cd34` ran with the
user's mailbox and no gate, in chat and, worse, in unattended agent runs where
nobody wrote the message that produced the call.

So the question moves from "is this name on a list" to "what is this call about
to do, and with whose authority".

## The read-only allowlist, and why it is that way round

A credentialed MCP call is gated unless its name begins with a verb that only
reads. Listing the *dangerous* verbs instead would be the natural way to write
this and the wrong one: the list is open-ended, a server can name its delete
endpoint anything, and the two failure directions are not symmetric. Guessing
"write" when it was a read costs one extra approval prompt. Guessing "read" when
it was a write sends the email. So an unrecognised verb is a write.

## Why not gate every credentialed call

Because the codebase already learned what that does — see the note above
`SENSITIVE_TOOLS` about prompting on every chart. An approval users click
through without reading is worse than no approval, since it launders consent.
Reads are the overwhelming majority of MCP traffic and the ones with no lasting
effect, so exempting them is what keeps the remaining prompts meaningful.

That exemption is withdrawn for unattended runs. In chat a human wrote the
message and is watching the answer arrive; in a scheduled agent run nobody did
and nobody is, so the reasoning that makes a read safe no longer holds.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Prefixes that mean the call only observes. Matched against the *original*
#: MCP tool name, lowercased, on a word boundary — `get_user` and `get-user`
#: match, `getaway_book` does not.
READ_ONLY_PREFIXES = (
    "get", "list", "search", "read", "fetch", "find", "query", "lookup",
    "describe", "show", "view", "check", "count", "download", "export",
    "retrieve", "browse", "inspect", "status",
)

_WORD_BREAK = ("_", "-", ".", ":", "/")


def looks_read_only(tool_name: str) -> bool:
    """
    Whether a tool's name claims it only reads.

    Deliberately a claim, not a proof. A server is free to name a destructive
    endpoint `list_and_purge`; nothing here can stop that, which is why this
    only ever *narrows* what gets gated and never widens what runs unattended.
    """
    name = (tool_name or "").strip().lower()
    for prefix in READ_ONLY_PREFIXES:
        if name == prefix:
            return True
        if name.startswith(prefix) and name[len(prefix):len(prefix) + 1] in _WORD_BREAK:
            return True
    return False


async def _server_for(tool_name: str):
    """The MCPServer behind an MCP tool name, or None if it is not one."""
    try:
        from mcp_integration.models import MCPServer
        from mcp_integration.tool_provider import decode_tool_name, is_mcp_tool

        if not is_mcp_tool(tool_name):
            return None
        decoded = decode_tool_name(tool_name)
        if decoded is None:
            return None
        return await MCPServer.objects.filter(id=decoded[0]).afirst()
    except Exception:  # noqa: BLE001
        logger.exception("[Permissions] Could not resolve server for %s", tool_name)
        return None


async def carries_credentials(tool_name: str) -> bool:
    """
    Whether this call would run with the user's own credentials injected.

    An unresolvable server counts as credentialed. The alternative is to let a
    lookup failure silently downgrade a gated call to an ungated one, which
    turns an outage into a permissions bypass.
    """
    from mcp_integration.tool_provider import is_mcp_tool

    if not is_mcp_tool(tool_name):
        return False

    server = await _server_for(tool_name)
    if server is None:
        return True
    return bool(server.required_credential_types or server.credential_env_map)


def strip_encoded_digest(suffix: str) -> str:
    """`send_email_ab12cd` -> `send_email`.

    `encode_tool_name` appends an 8-character content digest so two tools whose
    names sanitise to the same string stay distinct. Undoing it is shared rather
    than reimplemented: the connector scope needs the same answer at dispatch,
    where the original name is no longer available.
    """
    head, sep, tail = (suffix or "").rpartition("_")
    if sep and len(tail) == 8:
        return head or suffix
    return suffix


async def _original_name(tool_name: str) -> str:
    """
    The tool's name on its own server, for the read-only test.

    The encoded form already carries a sanitised copy of it, so this reads the
    suffix rather than paying for a catalogue fetch: `mcp__7__send_email_ab12cd`
    -> `send_email`. Falls back to the whole encoded name, which fails closed
    because it will not match any read-only prefix.
    """
    from mcp_integration.tool_provider import decode_tool_name

    decoded = decode_tool_name(tool_name)
    if decoded is None:
        return tool_name
    return strip_encoded_digest(decoded[1])


def mcp_reads_only(tool_name: str) -> bool:
    """Whether this is an MCP call whose *name* claims it only observes.

    Synchronous and I/O-free on purpose: the caller is the dispatch planner in
    `chat/turn/agent.py`, which runs once per call per turn and must not grow a
    database round trip to decide how to schedule work.

    This is the same claim `default_policy` already trusts to decide whether a
    call is gated, applied to a weaker question. The two failure modes are not
    comparable, which is why reusing it here is defensible where widening the
    approval exemption would not be: mis-reading `list_and_purge` as a read
    means an unapproved deletion when it decides *gating*, but only means that
    a deletion which was going to happen in this turn anyway overlapped a
    sibling call when it decides *scheduling*. Nothing about a name can make a
    call happen that was not already issued.

    Returns False for built-in tools — those declare `parallel` on `@tool()`,
    which is a statement by the person who wrote them and beats any guess.
    """
    from mcp_integration.tool_provider import decode_tool_name, is_mcp_tool

    if not is_mcp_tool(tool_name):
        return False
    decoded = decode_tool_name(tool_name)
    if decoded is None:
        # Fails closed: an unparseable name stays serial.
        return False
    return looks_read_only(strip_encoded_digest(decoded[1]))


async def is_remembered(tool_name: str, context: dict[str, Any]) -> bool:
    """Whether the user has already said "always allow" for this tool."""
    from django.db.models import Q

    from chat.models import ToolPermission

    user_id = context.get("user_id")
    if user_id is None:
        # A guest has no account to remember a decision against, and a saved
        # decision keyed on nothing would apply to every guest at once.
        return False

    session_key = str(context.get("session_id") or "")[:64]
    try:
        return await ToolPermission.objects.filter(
            Q(session_key="") | Q(session_key=session_key),
            user_id=user_id,
            tool_name=tool_name[:160],
        ).aexists()
    except Exception:  # noqa: BLE001
        logger.exception("[Permissions] Could not read saved decisions")
        return False


async def default_policy(name: str, args: dict, context: dict[str, Any]) -> bool:
    """
    Chat's policy: gate credentialed MCP calls that are not plainly reads.

    Returns False for built-in tools — those are covered by `SENSITIVE_TOOLS`,
    which `tools_node` checks first and which carries reasoning this cannot see.
    """
    if not await carries_credentials(name):
        return False
    if looks_read_only(await _original_name(name)):
        return False
    return not await is_remembered(name, context)


async def unattended_policy(name: str, args: dict, context: dict[str, Any]) -> bool:
    """
    The policy for runs no human is watching: every credentialed call is gated.

    The read exemption in `default_policy` is paid for by a human having written
    the message and being there to see the answer. Neither is true here, so a
    read of the user's mailbox by a schedule at 3am is exactly the thing worth
    asking about.
    """
    if not await carries_credentials(name):
        return False
    return not await is_remembered(name, context)


async def never(name: str, args: dict, context: dict[str, Any]) -> bool:
    """Gate nothing. For `autonomy='full'`, where the user has said so."""
    return False
