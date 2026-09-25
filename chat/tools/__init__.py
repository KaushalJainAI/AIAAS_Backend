"""
Shared tool registry for agentic execution.

Importing this package is what populates the registry: each submodule declares
its own tools with `@tool(...)`, so a tool's schema, its implementation and its
availability rule are one block of code rather than three lists that have to be
kept in step by hand.

What lives where:

  web           the open web — search, research, fetch, scrape
  knowledge     the user's knowledge bases, through the RAG tiers
  memory        durable facts about the user, across sessions
  planning      the run's own plan (`update_todos`)
  conversation  reading back from this session's own record
  agents        finding and running the user's saved agents
  authoring     creating and editing saved agents, through AgentSerializer
  sandbox       the wasm Python sandbox
  artifacts     the sandboxed-iframe HTML renderer
  charts        `render_chart` — data and a spec, drawn by the frontend
  ask           `ask_user`: an agent run records a question and proceeds
  vision        the `ask_vision` surface over `chat.vision`
  files         the agent's virtual filesystem over `inference.vfs`
   office        .pptx / .xlsx / .docx rendered from a spec into that filesystem
  media         `generate_image`, billed to the user and saved into their files
  browser       a remote Chromium: `browse_page` reads, `browser_act` acts (domain-scoped)
+  voice         transcription and speech, behind one-door engines
+  docs          `ocr_document`: scanned PDFs and photos as text, rows or fields
+  esign         documents out for e-signature, completed by webhook
+  talk          one tool set for Slack, WhatsApp, Teams, SMS and Telegram
+  publish       hosted pages: snapshots shareable by link (`link`/`platform`/`public`)
   internal      this platform's own API, called as the user
  clock         wall-clock time
+  data          SQL over the user's databases (read, and writes where allowed)
+  apicaller     one generic caller for the user's HTTP APIs
   fetch         `download_file`: a URL the user named, kept as their file
  runs          the user's jobs: `list_user_runs` plus the progress block
  workspace     the platform itself: `extract_data`, `notify_user` now, and
                the reminder trio for later (`schedule_notification` and co.)
  google        native Gmail / Drive / Sheets / Calendar connector tools

Connector tools (`google`) *are* registered here, and carry `connector=` so the
Connections card they belong to still governs them — see
`mcp_integration/native.py`. MCP tools are not registered here. They are minted at runtime from a
third-party catalogue, so they are resolved on every call and every listing
instead — see `mcp_integration.tool_provider`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from . import (  # noqa: F401  — imported for their registration side effect
    agents,
    apicaller,
    artifacts,
    ask,
    authoring,
    browser,
    charts,
    clock,
    code,
    compute,
    conversation,
    dashboards,
    data,
    docs,
    esign,
    eval_manager,
    fetch,
    files,
    google,
    internal,
    knowledge,
    media,
    memory,
    missions,
    notion,
    office,
    planning,
    publish,
    runs,
    sandbox,
    talk,
    tasks,
    vision,
    voice,
    web,
    workspace,
)
from .registry import (
    Tool,
    all_tools,
    effect_of,
    get,
    names_with_effect,
    parallel_names,
    schemas,
    sensitive_names,
    tool,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AVAILABLE_TOOLS",
    "CHAT_AUTHORING_TOOLS",
    "CHAT_DELEGATION_TOOLS",
    "CHAT_ORCHESTRATOR_EXTRA",
    "ORCHESTRATOR_REFUSAL",
    "READ_ONLY_TOOLS",
    "SENSITIVE_TOOLS",
    "Tool",
    "all_tools",
    "chat_orchestrator_allowed",
    "disabled_tools_for",
    "effect_of",
    "execute_chat_tool",
    "execute_tool",
    "get",
    "get_available_tools",
    "live_connectors",
    "names_with_effect",
    "PARALLEL_TOOLS",
    "schemas",
    "tool",
]

#: The full built-in catalogue, ungated. Callers that need one user's view want
#: `get_available_tools`; this is for code that filters by its own rules, like
#: an agent's allow-list.
AVAILABLE_TOOLS: List[Dict[str, Any]] = schemas()

#: Tools that require Human-In-The-Loop approval before execution, derived from
#: the tools' own `sensitive=True`. It is a short list on purpose — see
#: `chat.permissions`, which gates by policy rather than by name, and covers the
#: MCP tools that could never appear in a list of names minted at build time.
#:
#: `execute_python` is deliberately absent, unlike in `agent_runtime`, where an
#: unattended agent asks before running code. Here a human wrote the message
#: that produced the call and is watching the answer arrive, and the sandbox has
#: no network, no filesystem and no imports that reach either — so the prompt
#: would be asking permission for arithmetic. `run_agent` *is* sensitive for the
#: mirror reason: it hands work to something the user is not watching.
#:
#: `render_html_artifact` is deliberately absent too. It renders inside a
#: sandboxed iframe with no network, no same-origin access and no session, so
#: there is nothing for a human to meaningfully approve — and prompting on every
#: chart would train users to click through approvals without reading them,
#: which is what makes the prompts on the remaining tools worthless.
SENSITIVE_TOOLS: List[str] = sensitive_names()

#: Tools that change nothing outside this process, derived from the tools' own
#: `effect="read"`. This is what `plan` mode offers and what the looser
#: autonomy levels are willing to run without asking.
#:
#: It is not the complement of `SENSITIVE_TOOLS`. The two answer different
#: questions — `sensitive` is "ask a watching human in chat", `effect` is "what
#: happens if nobody was asked" — and they disagree in both directions:
#: `execute_python` is not sensitive here but is gated in unattended agent
#: runs, while `write_file` is sensitive and yet recoverable from the recycle
#: bin. Collapsing them into one flag is what would force the middle rungs of
#: the autonomy ladder to guess.
READ_ONLY_TOOLS: frozenset[str] = names_with_effect("read")

#: Chat is the orchestrator and cannot be configured like a subagent, so it
#: holds only basic tools — everything else lives in subagents the user
#: configures explicitly. Decentralised orchestration (2026-09-25): the chat
#: turn reads, plans and delegates; a worker's *actions* still meet its own
#: gates at run time.
#:
#: What that means concretely: `get_available_tools` (chat's toolbox) offers
#: `effect="read"` tools, `ALWAYS_AVAILABLE`-style infrastructure, and the
#: names below — delegation, authoring, memory and missions. Anything else
#: (file writes, sends, publishes, renders, browser acts, ...) is withheld
#: here and refused in `execute_tool`, with a message telling the model to
#: delegate to a specialist via files rather than retrying.
#:
#: Delegation: how the manager reaches its workers. `search_agents` to find
#: them, `run_agent` / `invoke_subagent` to run them, `get_agent_run` to check
#: a long run, `answer_subagent` to answer one that stopped to ask, and the
#: detached `start_tasks` / `wait_tasks` / `task_status` / `steer_task` /
#: `stop_task` / `revert_task` trio the coding lead uses.
CHAT_DELEGATION_TOOLS: frozenset[str] = frozenset({
    'search_agents', 'run_agent', 'get_agent_run', 'invoke_subagent',
    'answer_subagent', 'start_tasks', 'wait_tasks', 'task_status',
    'steer_task', 'stop_task', 'revert_task',
})

#: Authoring: building the team is the manager's job. Chat-only, through
#: `AgentSerializer`, so the ownership checks still bound what it writes.
CHAT_AUTHORING_TOOLS: frozenset[str] = frozenset({
    'create_agent', 'update_agent',
})

#: The rest of the orchestrator's extra set beyond reads and infrastructure:
#: durable facts about the person (agents read memory but cannot write it, so
#: dropping these from chat would make memory read-only for everyone), and
#: `start_mission` (`mission_status` / `complete_mission` / `report_progress`
#: are already `ALWAYS_AVAILABLE`; starting the mission is orchestration).
CHAT_ORCHESTRATOR_EXTRA: frozenset[str] = frozenset(
    set(CHAT_DELEGATION_TOOLS)
    | set(CHAT_AUTHORING_TOOLS)
    | {'remember_about_user', 'forget_about_user', 'start_mission'}
)

#: What the model is told when it reaches for a tool the orchestrator withholds.
ORCHESTRATOR_REFUSAL = (
    'Not available in this chat turn: the orchestrator reads, plans and delegates, '
    'and critical actions live in subagents. Use search_agents to find a specialist '
    '(Analyst, Slides, Writer, or one you built), hand it the job with run_agent or '
    'invoke_subagent passing findings via files, and report back what it produced. '
    'Do not retry this tool directly.'
)


def chat_orchestrator_allowed(name: str) -> bool:
    """Whether the chat orchestrator may call `name` at all.

    Read tools, infrastructure (`ALWAYS_AVAILABLE`-style names are all read or
    own-state) and the explicit extra set above. Everything else — including
    every MCP tool whose name does not claim to read — is for a configured
    subagent. Unknown names fail closed: an MCP tool never appears here.
    """
    from agents.agent.runtime import ALWAYS_AVAILABLE, RETRIEVAL_TOOLS

    if name in CHAT_ORCHESTRATOR_EXTRA:
        return True
    if name in ALWAYS_AVAILABLE or name in RETRIEVAL_TOOLS:
        return True
    try:
        from .registry import effect_of
    except Exception:  # noqa: BLE001
        return False
    try:
        return effect_of(name) == 'read'
    except Exception:  # noqa: BLE001
        return False

#: Tools that may be dispatched at the same time as their siblings in one turn,
#: declared per tool via `@tool(..., parallel=True)`.
#:
#: The model issues every call in a turn before it has seen any result, so no
#: call in a batch can depend on another one — overlapping them is safe by
#: construction for anything that only reads. The set is an allow-list rather
#: than a deny-list because the unsafe cases are unsafe for reasons no name
#: reveals: `execute_python` captures output by swapping the process-global
#: `sys.stdout`, and an MCP tool is a third-party server that never gets to
#: carry this flag at all. Not in the set means serial, which is what the
#: runtime did for everything before this existed.
PARALLEL_TOOLS: frozenset = parallel_names()


#: Where the MCP descriptor list is parked inside a turn's memo. Namespaced so
#: the memo can hold other per-turn work later without two writers colliding.
_MCP_MEMO_KEY = "mcp_descriptors"
_NATIVE_MEMO_KEY = "native_connectors"


async def live_connectors(
    user_id: int | None, memo: Dict[str, Any] | None = None,
) -> Dict[str, int]:
    """Native connector cards live for this user, memoised per turn if asked.

    Memoised on the same terms as the MCP half: whether a card is switched on
    or a credential exists does not change while a turn is running, and the
    listing is called before every model call. `execute_tool` reads it fresh,
    so a switch flipped mid-run still refuses the next call.
    """
    if memo is not None and _NATIVE_MEMO_KEY in memo:
        return memo[_NATIVE_MEMO_KEY]
    from mcp_integration.native import live_native_connectors

    live = await live_native_connectors(user_id)
    if memo is not None:
        memo[_NATIVE_MEMO_KEY] = live
    return live


async def _requirement_met(
    requirement: str,
    user_id: int | None,
    memory_enabled: bool,
    session_key: str | None,
    file_scope: Any = None,
) -> bool:
    """
    Whether a tool's precondition holds for this caller.

    Unmet means the tool is not offered at all. An advertised tool that cannot
    run is worse than one never offered, because the model plans around it and
    then has to explain the failure.
    """
    if requirement == "memory":
        return memory_enabled
    if requirement == "spill":
        return await conversation.has_spilled_output(user_id, session_key)
    if requirement == "files":
        # Met when the caller brought a scope. Chat now does — `chat_scope`
        # gives every turn the whole tree to read and `/Chat/` to write into —
        # and a caller that brought none still gets the tools withheld rather
        # than offered and refusing.
        #
        # This used to be an unconditional `False`, and the reasoning was
        # sound at the time: the file tools chat *used* to have reached the
        # host filesystem, and re-admitting them would have undone that
        # deletion quietly. What changed is the tools, not the decision. These
        # address rows in the user's own `Folder`/`Document` tree through
        # `inference/vfs.py` and cannot name a path on any disk. The half of
        # the old rule that still holds — no host filesystem from a chat turn —
        # is still pinned by `test_rework.py::RemovedCapabilityTests`.
        #
        # The agent toolbox does not consult requirements at all: it filters
        # `AVAILABLE_TOOLS` by the names its grants unlock, so `fileOps` is
        # what turns these on there, and that is unchanged.
        return file_scope is not None
    if requirement == "browser":
        # Configuration, not a credential: with no engine there is nothing to
        # offer, and a browser tool that always refuses is one the model plans
        # around and then has to explain.
        from browsing.engine import browser_available

        return browser_available()
    if requirement == "stt":
        from voice.stt import stt_available

        return stt_available()
    if requirement == "tts":
        from voice.tts import tts_available

        return tts_available()
    if requirement == "esign":
        from esign.provider import esign_available

        return esign_available()
    if requirement == "agent_run":
        # Never met here: this function answers for chat, where the person is
        # reading the reply and a question is just the reply. The agent
        # toolbox does not consult requirements; `ask_user` reaches agents by
        # being in `ALWAYS_AVAILABLE`.
        return False
    if requirement == "workspace":
        from workspaces.engine import workspace_available

        return workspace_available()
    if requirement == "vision":
        if user_id is None:
            return False  # no user, no credential, no witness
        try:
            from ..vision import witness_available

            return await witness_available(user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not resolve vision witness for user {user_id}: {e}")
            return False
    return True


async def disabled_tools_for(user_id: int | None) -> frozenset[str]:
    """Tools this user has switched off in the tool library.

    A second axis from `requires`, and deliberately so: a requirement is a fact
    about the turn (no witness resolved, nothing spilled yet) that the user
    cannot decide, while this is a decision they made about their workspace.
    Both end the same way - the tool is not offered - because an advertised
    tool that cannot run is worse than one never offered.

    A failure here means "nothing is switched off", never "everything is": a
    cache miss or a migration not yet applied must not silently strip an
    agent's toolbox mid-run.
    """
    if not user_id:
        return frozenset()
    try:
        from tools_config.overlay import adisabled_names

        return await adisabled_names(user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not read tool settings for user {user_id}: {e}")
        return frozenset()


async def get_available_tools(
    user_id: int | None,
    memory_enabled: bool = True,
    session_key: str | None = None,
    mcp_memo: Dict[str, Any] | None = None,
    file_scope: Any = None,
) -> List[Dict[str, Any]]:
    """
    Return the orchestrator's tool list for this user: built-in tools it may
    hold whose requirements are met, plus the read-only MCP half.

    Chat is the orchestrator and cannot be configured like a subagent, so it
    holds only basic tools — reads, infrastructure, delegation, authoring,
    memory and missions (`chat_orchestrator_allowed`). Critical actions live
    in subagents the user configures explicitly. Safe to call on every agent
    turn (MCP tool lists are cached in Redis).

    `mcp_memo` is scratch space belonging to one turn. Given one, the MCP half
    of the list is resolved on the first call and reused on every later call
    that shares it — which is what makes this safe to call in a loop. The Redis
    cache underneath is not enough on its own: even a hit costs a database
    read per connection, and a lapsed entry still answers from the stale copy
    while refreshing behind the turn (see `mcp_integration/tool_cache.py`).
    What a memo must *not* cover is the
    built-in half, which is why it is passed to the MCP call alone: whether a
    tool's requirement is met can change mid-run (a result spills, and
    `read_tool_output` becomes real), and freezing that would withhold a tool
    the run has just earned.
    """
    disabled = await disabled_tools_for(user_id)
    live = await live_connectors(user_id, mcp_memo)

    tools: List[Dict[str, Any]] = []
    for entry in all_tools():
        if entry.name in disabled:
            continue
        if not chat_orchestrator_allowed(entry.name):
            # Decentralised orchestration: critical tools live in subagents,
            # not in the unconfigurable orchestrator. Withheld rather than
            # left to refuse at call time, for the usual reason — an
            # advertised tool the model plans around and then cannot run.
            continue
        if entry.connector is not None and entry.connector not in live:
            # The card is off, or the user has not connected the account it
            # needs. Not offered — an advertised tool that can only answer
            # "not connected" is worse than one never offered.
            continue
        if entry.requires and not await _requirement_met(
            entry.requires, user_id, memory_enabled, session_key, file_scope
        ):
            continue
        tools.append(entry.schema)

    if user_id is None:
        return tools

    if mcp_memo is not None and _MCP_MEMO_KEY in mcp_memo:
        tools.extend(mcp_memo[_MCP_MEMO_KEY])
        return tools

    try:
        from mcp_integration.tool_provider import MCPToolProvider

        mcp_tools = await MCPToolProvider.get_openai_tool_descriptors(user_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not load MCP tools for user {user_id}: {e}")
        # Deliberately not memoised. A failure here is usually transient — a
        # cold subprocess that timed out, Redis blinking — and remembering it
        # would cost the run every connector it has for the rest of the turn.
        return tools

    # Orchestrator holds read-only MCP tools; writes live in subagents. The
    # name is a third party's claim, so this only ever narrows — a write
    # misnamed as a read still meets the approval gate at dispatch.
    mcp_tools = [
        d for d in mcp_tools
        if isinstance(d, dict)
        and _mcp_name_reads(d.get('function', {}).get('name', ''))
    ]

    if mcp_memo is not None:
        mcp_memo[_MCP_MEMO_KEY] = mcp_tools
    tools.extend(mcp_tools)
    return tools


def _mcp_name_reads(func_name: str) -> bool:
    """Whether an encoded MCP tool name claims to only read. Fails closed."""
    try:
        from mcp_integration.tool_provider import decode_tool_name

        from .permissions import looks_read_only, strip_encoded_digest

        decoded = decode_tool_name(func_name)
        original = (
            strip_encoded_digest(decoded[1]) if decoded is not None else func_name
        )
        return looks_read_only(original)
    except Exception:  # noqa: BLE001
        return False


async def execute_chat_tool(
    func_name: str, args: Dict[str, Any], context: Dict[str, Any]
) -> str:
    """`execute_tool` behind the orchestrator's scope — chat's dispatcher.

    The scope is re-checked here and not only when advertising, because a model
    that saw a tool earlier in the transcript will call one it was not offered
    this turn. It is a separate door from `execute_tool` on purpose:
    `AgentToolbox.dispatch` ends in `execute_tool` after its own grant checks,
    and a subagent is exactly where the writes, sends and renders now live —
    putting this check in the shared dispatcher refuses every one of them.
    """
    try:
        from mcp_integration.tool_provider import is_mcp_tool

        is_mcp = is_mcp_tool(func_name)
    except Exception:  # noqa: BLE001
        is_mcp = False

    if is_mcp:
        if not _mcp_name_reads(func_name):
            return f"Error: {ORCHESTRATOR_REFUSAL}"
    elif get(func_name) is not None and not chat_orchestrator_allowed(func_name):
        return f"Error: {ORCHESTRATOR_REFUSAL}"
    return await execute_tool(func_name, args, context)


async def execute_tool(
    func_name: str, args: Dict[str, Any], context: Dict[str, Any]
) -> str:
    """Execute a tool by name and return its string response.

    Unscoped: shared by the agent toolbox (after its grant checks) and the
    direct `/execute-tool/` endpoint. Chat's model dispatches through
    `execute_chat_tool`, which adds the orchestrator's scope.
    """
    # Deliberately two try blocks. Wrapping the *import* and the dispatch
    # together meant any failure inside mcp_integration — an import error, a
    # misconfigured app — returned "Error executing MCP tool web_search" for
    # every built-in and never reached the registry below. A broken MCP
    # subsystem must cost the user their MCP tools, not all of them.
    try:
        from mcp_integration.tool_provider import MCPToolProvider, is_mcp_tool

        is_mcp = is_mcp_tool(func_name)
    except Exception as e:  # noqa: BLE001
        logger.error(f"MCP tool provider unavailable for {func_name}: {e}")
        is_mcp = False

    if is_mcp:
        try:
            return await MCPToolProvider.execute(func_name, args, context.get("user_id"))
        except Exception as e:  # noqa: BLE001
            logger.error(f"MCP dispatch failed for {func_name}: {e}")
            return f"Error executing MCP tool {func_name}: {str(e)}"

    entry = get(func_name)
    if entry is None:
        return f"Error: Tool '{func_name}' is not recognized."

    # Re-checked at dispatch and not only at advertising time, for the reason
    # `AgentToolbox.dispatch` re-checks grants: a model that saw a tool earlier
    # in the transcript - or remembers the name from training - will call one it
    # was not offered this turn, and "we didn't mention it" is not a switch.
    if func_name in await disabled_tools_for(context.get("user_id")):
        return (
            f"Error: '{func_name}' is switched off in this workspace's tool "
            f"settings. Do not try it again; solve the task with the tools you "
            f"have, or say what you would need."
        )

    if entry.connector is not None and entry.connector not in await live_connectors(
        context.get("user_id")
    ):
        return (
            f"Error: '{func_name}' needs a connection that is switched off or not "
            f"connected for this user. Do not try it again; tell the user to turn it "
            f"on or connect it on the Connections page if they want it used."
        )

    try:
        return await entry.run(args, context)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error executing tool {func_name}: {e}")
        return f"Error executing tool {func_name}: {str(e)}"
