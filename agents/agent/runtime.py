"""
The agent runtime: what actually executes a saved agent.

Until this module existed an agent was a row — the builder saved a complete
`AgentConfig` and nothing could run it, so every `runs` count was zero. See
docs/AGENT_TEMPLATES.md §8, Phase 1.

**The one rule this module exists to keep.** The permissions screen an installer
sees is rendered from `tool_grants` / `guardrails` / `agent_context`. If the
runtime read anything else, or defaulted a missing grant to "allowed", the
screen would be a promise nothing enforces. So the grant map below is the only
place a tool becomes reachable, and `AgentToolbox.dispatch` re-checks the grant
at call time rather than trusting that an ungranted tool was merely never
advertised — a model that remembers a tool name from its training data will
happily call one it was not offered.

**Why it borrows the chat loop.** `chat.agent` already threads tool calls as a
proper transcript, streams to a sink, and pauses on `interrupt()` for approval.
Forking it would mean maintaining two loops that must agree on message
threading, which is exactly the thing the chat rewrite was about getting right.
Instead `TurnContext` takes three optional hooks — `tool_source`,
`tool_dispatch`, `sensitive_tools` — and this module supplies all three. Chat
turns pass none and behave exactly as before.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from llm.pricing import format_usd
from typing import Any

from asgiref.sync import sync_to_async
from django.utils import timezone

# Pure data — no models, no Django app registry — so this is safe at module
# scope even though the rest of the agent layer is imported lazily.
from agents import connector_scope
# The permissions vocabulary lives in `agents/grants.py` so that reading the
# policy never requires importing the runtime. Re-imported here because the
# runtime enforces every table in it.
from agents.grants import (  # noqa: F401 — re-exported for existing callers
    ALWAYS_AVAILABLE,
    AUTONOMY_LADDER,
    CALLERS,
    CODE_COMMAND_CLASSES,
    GRANT_TOOLS,
    RETRIEVAL_TOOLS,
    UNATTENDED_CALLERS,
    UNSERVED_GRANTS,
)

logger = logging.getLogger(__name__)


class AgentRunRefused(Exception):
    """The run was rejected before any model call — a guardrail said no."""


# ── Tools ────────────────────────────────────────────────────────────────────
#
# Code execution (`execute_python`) is declared and implemented in the chat
# tool registry, where it runs the same `sandbox` package as every other
# caller. This module keeps only what is actually its own: the grant that
# decides whether an agent may reach it.


@dataclass(slots=True)
class AgentToolbox:
    """The tools one agent may see and use, derived from its grants alone."""

    grants: dict[str, bool]
    user_id: int
    #: Grants the builder set that this runtime will not serve. Surfaced so the
    #: caller can tell the user, instead of the agent silently lacking a tool
    #: its brief assumes it has.
    unserved: tuple[str, ...] = ()
    #: The `inference.vfs.FileScope` this run may address, or None when
    #: `fileAccess` is 'none'. Built outside this class because it touches the
    #: database (a scoped agent's home folder is created on demand) and this
    #: constructor is called from async code.
    file_scope: Any = None

    #: `plan` autonomy: withhold everything that could change anything, so the
    #: run can only look and report. Enforced by *removing the tools* rather
    #: than by gating them, because a gate the user can approve is not a plan
    #: mode — it is `review` with a different label. With nothing mutating left
    #: to offer there is also nothing to approve, which is why
    #: `approval_policy_for('plan')` is `never`.
    read_only: bool = False

    #: The run's thread id, which is also the scope key the archive is written
    #: under. Needed because whether the retrieval tools are offered is a
    #: question about *this run's* stored text, not about the user.
    session_key: str = ''

    #: Archives from other runs this one may read — a worker's parent. Part of
    #: the toolbox because it decides whether the retrieval tools are *offered*:
    #: a worker whose own archive is empty but whose parent's is not still needs
    #: them.
    archive_scopes: tuple[str, ...] = ()

    #: Which connections this agent may reach *and which of their tools*, or
    #: None for "any the user has". The second axis to the `mcp` grant, exactly
    #: as `file_scope` is the second axis to `fileOps`: the grant says *may it
    #: reach connectors at all*, this says *which ones, and how much of each*.
    #:
    #: None rather than "all of them" is load-bearing for the same reason it is
    #: in `kb_scope_for`: this selection existed in the builder long before
    #: anything read it, so turning enforcement on must not silently empty the
    #: toolbox of every agent that never made a choice. An empty selection is
    #: therefore *unrestricted*, and only a deliberate non-empty one narrows.
    #:
    #: Widened from a tuple of ids to a `ConnectorScope` on 2026-09-03: picking
    #: a mailbox used to grant sending and deleting along with reading, because
    #: there was no third field to say otherwise. See `agents/connector_scope.py`.
    mcp_scope: Any = None

    #: The exact built-in tools this agent may use, or `()` for "everything
    #: its grants unlock". The third axis after the grant (may it at all) and
    #: the scope (which rows): *which tools*, so one agent can hold `mcp` for
    #: reading a mailbox without also carrying forty descriptors it never
    #: calls. Empty is unrestricted, the same default every other scope takes,
    #: because the field arrives after the agents that predate it.
    #:
    #: `ALWAYS_AVAILABLE` and `RETRIEVAL_TOOLS` are never narrowed by it: an
    #: agent that may not keep its own plan or read back its own archived
    #: output is not narrower, only more forgetful.
    tool_scope: tuple[str, ...] = ()

    #: Per-tool allow/ask/deny for built-in tools, or `{}`.
    #:
    #: The fourth axis after the grant (may it at all), the scope (which
    #: rows) and `tool_scope` (which tools): *how* this agent may use each
    #: one. Empty is today's behaviour — the grants, `toolScope` and the
    #: autonomy ladder decide alone — which is what every agent saved before
    #: the field existed carries.
    #:
    #: `deny` withholds the tool here and refuses it in `dispatch`; `ask`
    #: and `allow` only move the approval gate (see
    #: `TurnContext.tool_permissions`) and never widen what the grants
    #: reach — an `allow` on a tool whose grant is off stays unoffered.
    tool_permissions: dict[str, str] = field(default_factory=dict)

    #: The MCP descriptors this run resolved, or None before the first pass.
    #: One toolbox serves one run, so this is a per-run memo: `descriptors` is
    #: called before *every* model call, and resolving connectors costs a
    #: database read per connection plus, on a cold cache, an `npx` start —
    #: paid before the first token, every iteration, for an answer that cannot
    #: change while the run is going.
    #:
    #: Only the MCP half is remembered. The built-in half above it is rebuilt
    #: each pass because it genuinely moves: `RETRIEVAL_TOOLS` appear the
    #: moment this run archives something, and a memo would withhold a tool
    #: the run has just earned.
    _mcp_descriptors: list[dict[str, Any]] | None = None

    #: Native connector cards live for this run (`icon_slug -> server id`), or
    #: None before the first read. Per-run for the reason `_mcp_descriptors`
    #: is; dispatch reads it too, and `execute_tool` re-checks the card fresh.
    _native_live: dict[str, int] | None = None

    #: The eval world this run is confined to (`eval/environment.py`), or None
    #: for every other caller. When set, three things change: `allowed_names`
    #: loses every tool with no fake version (fail closed), `mcp_allowed` goes
    #: False (MCP tools are never simulated), and `dispatch` answers simulated
    #: tools from the world instead of calling anything real. Duck-typed on
    #: purpose — this module must not import `eval`, which imports this module
    #: for `run_agent`.
    environment: Any = None

    @classmethod
    def for_agent(cls, agent, user_id: int, *, file_scope: Any = None,
                  read_only: bool = False, session_key: str = '',
                  archive_scopes: tuple[str, ...] = (),
                  environment: Any = None) -> AgentToolbox:
        grants = {k: bool(v) for k, v in (agent.tool_grants or {}).items()}
        unserved = tuple(sorted(g for g in UNSERVED_GRANTS if grants.get(g)))
        return cls(grants=grants, user_id=user_id, unserved=unserved,
                   file_scope=file_scope, read_only=read_only,
                   session_key=session_key, archive_scopes=archive_scopes,
                   mcp_scope=connector_scope.for_agent(agent),
                   tool_scope=tool_scope_for(agent),
                   tool_permissions=tool_permissions_for(agent),
                   environment=environment)

    @property
    def allowed_names(self) -> frozenset[str]:
        # `RETRIEVAL_TOOLS` are always dispatchable and only conditionally
        # *offered* (see `descriptors`). The split matters: the condition is a
        # database read, and a model that names a tool it saw a moment ago must
        # not be refused because a row expired between the two.
        names = set(ALWAYS_AVAILABLE) | set(RETRIEVAL_TOOLS)
        for grant, tools in GRANT_TOOLS.items():
            if self.grants.get(grant):
                names.update(tools)
        if self.grants.get('mcp'):
            # Every native connector tool the registry knows. Which of them are
            # live and in scope is decided in `descriptors` / `dispatch`, which
            # can await; this property cannot. Unlike MCP, these survive `plan`
            # through the `READ_ONLY_TOOLS` intersection below, because each
            # one declares its own effect and a declared read is a read.
            from chat.tools.registry import connector_tool_names
            names.update(connector_tool_names())
        if self.tool_scope:
            # Narrowed to the chosen tools, plus the infrastructure ones that
            # are never a capability: the plan, the clock, the archive.
            names &= set(self.tool_scope) | set(ALWAYS_AVAILABLE) | set(RETRIEVAL_TOOLS)
        if self.tool_permissions:
            # Refused per tool, not per grant. Withheld rather than left to
            # refuse at call time, for the reason the file-scope block below
            # gives — with `dispatch` re-checking anyway, because advertising
            # is not access control.
            names -= {n for n, m in self.tool_permissions.items() if m == 'deny'}
        if self.read_only:
            from chat.tools import READ_ONLY_TOOLS
            names &= set(READ_ONLY_TOOLS)
        if names & set(GRANT_TOOLS['browser']):
            # Granted, but nothing to drive: not offered, rather than offered
            # and refusing on every call.
            from browsing.engine import browser_available

            if not browser_available():
                names -= set(GRANT_TOOLS['browser'])
        if names & set(GRANT_TOOLS['voice']):
            from voice.stt import stt_available
            from voice.tts import tts_available

            if not stt_available():
                names -= {'transcribe_audio'}
            if not tts_available():
                names -= {'text_to_speech'}
        if names & set(GRANT_TOOLS['esign']):
            from esign.provider import esign_available

            if not esign_available():
                names -= set(GRANT_TOOLS['esign'])
        if names & set(GRANT_TOOLS['compute']):
            from workspaces.engine import workspace_available

            if not workspace_available():
                names -= set(GRANT_TOOLS['compute'])
        if names & set(GRANT_TOOLS['shell']):
            from workspaces.engine import workspace_available

            if not workspace_available():
                names -= set(GRANT_TOOLS['shell'])
        if self.file_scope is None:
            # Granted `fileOps` but `fileAccess='none'` — the two settings
            # disagree, and the safe reading is the narrower one. Withheld
            # rather than left to refuse at call time: an advertised tool that
            # cannot run is worse than one never offered, because the model
            # plans around it and then has to explain the failure.
            # `run_python_on_files` is the file-writing half of `codeExecution`
            # (`execute_python` itself is read-only and stays): same rule.
            names -= (
                set(GRANT_TOOLS['fileOps'])
                | set(GRANT_TOOLS['office'])
                | set(GRANT_TOOLS['media'])
                | {'run_python_on_files'}
                # File-backed tools outside `fileOps`: transcription reads
                # audio and writes the transcript; speech saves audio; OCR
                # reads documents; a signature sends one.
                | {'transcribe_audio', 'text_to_speech', 'ocr_document',
                   'request_signature'}
            )
        if self.environment is not None:
            # An eval world has no fake version of these tools, so the run
            # does not get them at all — fail closed, both when offering and
            # when the model names one anyway (dispatch re-checks below).
            names -= self.environment.withheld_names(names)
        return frozenset(names)

    @property
    def mcp_allowed(self) -> bool:
        # Withheld wholesale under `plan`. An MCP tool's name is minted at
        # runtime from a third-party catalogue, so nothing here can tell a
        # read from a write on that server — and `looks_read_only` is a claim
        # the server makes about itself, which is thin evidence to offer a
        # mode whose entire promise is that nothing will change. This is read
        # by `descriptors` and by `dispatch`, so the withdrawal covers both
        # advertising the tools and running one the model named anyway.
        if self.read_only:
            return False
        # And withheld wholesale inside an eval world: MCP tools are never
        # simulated, so offering them would hand the run a live third-party
        # service. Simulated *native* tools (Gmail, Calendar, …) are not MCP
        # tools and are unaffected — they arrive through `allowed_names`.
        if self.environment is not None:
            return False
        return bool(self.grants.get('mcp'))

    def mcp_call_allowed(self, name: str) -> tuple[bool, str]:
        """Whether this agent may make this namespaced MCP call, and why not.

        Separate from `mcp_allowed` because they answer different questions and
        both have to be asked at dispatch: the grant says whether connectors are
        reachable at all, this says whether *this* connection was chosen and
        whether *this* tool is inside the scope chosen for it. Withholding the
        descriptor is not access control — the model can name a tool it saw in
        an earlier turn, or one a sibling agent mentioned.

        An unparseable name is refused rather than allowed. The only way to get
        here with one is a name that passed `is_mcp_tool` (so it starts `mcp__`)
        but does not carry a server id, which is not a tool that exists; letting
        it through would make the prefix alone enough to escape the selection.

        The second half of the return is the refusal, because a model told only
        "denied" retries the same call until the iteration cap ends the run.
        """
        if self.mcp_scope is None:
            return True, ''
        from mcp_integration.tool_provider import decode_tool_name

        decoded = decode_tool_name(name)
        if decoded is None:
            return False, 'that connection'
        server_id, encoded_tool = decoded
        if not self.mcp_scope.server_allowed(server_id):
            return False, 'that connection'
        # The encoded middle section carries a sanitised copy of the tool's own
        # name plus an 8-char digest; `_original_name` is where that is undone,
        # and it is shared rather than reimplemented here.
        from chat.tools.permissions import strip_encoded_digest

        original = strip_encoded_digest(encoded_tool)
        if not self.mcp_scope.tool_allowed(server_id, original):
            return False, self.mcp_scope.describe(server_id)
        return True, ''

    async def native_live(self) -> dict[str, int]:
        if self._native_live is None:
            from chat.tools import live_connectors
            self._native_live = await live_connectors(self.user_id)
        return self._native_live

    async def native_call_allowed(self, name: str) -> tuple[bool, str]:
        """Whether this native connector tool is live and inside the scope.

        The native half of `mcp_call_allowed`, keyed the same way — by the
        card's server id — so one stored `connectors` selection governs a
        connector whichever way its tools are served.
        """
        from chat.tools.registry import connector_of

        slug = connector_of(name)
        if slug is None:
            return True, ''
        if self.environment is not None and self.environment.simulates(name):
            # An eval world answers this tool from fixtures, so no card needs
            # to be live and no credential held — but the agent's connector
            # scope still applies. Skipping it evaluated a stronger agent than
            # the one that runs: "Gmail, read-only" could send, and an agent
            # scoped to one connector got every simulated one.
            from mcp_integration.native import native_server_ids

            server_id = (await native_server_ids(self.user_id)).get(slug)
            if server_id is None:
                return False, 'that connection'
        else:
            server_id = (await self.native_live()).get(slug)
            if server_id is None:
                return False, 'that connection (it is switched off or not connected)'
        if self.mcp_scope is None:
            return True, ''
        if not self.mcp_scope.native_tool_allowed(server_id, name):
            if not self.mcp_scope.server_allowed(server_id):
                return False, 'that connection'
            return False, self.mcp_scope.describe(server_id)
        return True, ''

    async def descriptors(self) -> list[dict[str, Any]]:
        """The tool list the model is offered this turn."""
        from chat.tools import AVAILABLE_TOOLS

        allowed = set(self.allowed_names)

        # Withheld until this run has archived something, exactly as chat's
        # `requires="spill"` does it and through the same predicate. An
        # advertised tool that cannot run is worse than one never offered: the
        # model plans around it and then has to explain the failure.
        from chat.tools.conversation import has_spilled_output

        if not await has_spilled_output(
            self.user_id, (self.session_key, *self.archive_scopes)
        ):
            allowed -= set(RETRIEVAL_TOOLS)

        # The workspace-wide switch from the tool library (`tools_config`).
        # It is a *second* subtraction rather than part of `allowed_names`
        # because it is a database read and that property is sync and called
        # from everywhere; `chat.tools.execute_tool` re-checks it at dispatch,
        # so a tool switched off between the listing and the call is refused
        # rather than run.
        from chat.tools import disabled_tools_for

        allowed -= set(await disabled_tools_for(self.user_id))

        from chat.tools.registry import connector_of

        for name in [n for n in allowed if connector_of(n) is not None]:
            # In an eval world a simulated tool needs no live card, but it
            # still passes the connector scope — `native_call_allowed` handles
            # both cases.
            if not (await self.native_call_allowed(name))[0]:
                allowed.discard(name)

        descriptors = [
            t for t in AVAILABLE_TOOLS
            if t.get('function', {}).get('name') in allowed
        ]

        if self.mcp_allowed:
            if self._mcp_descriptors is not None:
                return descriptors + self._mcp_descriptors
            try:
                from mcp_integration.tool_provider import MCPToolProvider
                scope = self.mcp_scope
                resolved = await MCPToolProvider.get_openai_tool_descriptors(
                    self.user_id,
                    None if scope is None else scope.server_ids,
                    # Narrowed by tool as well as by connection. The filter
                    # runs where the *original* names are, because that is
                    # the only place they exist — everything downstream sees
                    # the encoded form.
                    None if scope is None else scope.tool_allowed,
                )
            except Exception:
                # A dead MCP server must degrade the agent, not fail the run.
                # Not remembered either: the usual cause is a cold subprocess
                # that timed out, and caching that would cost the run every
                # connector it has for the rest of its iterations.
                logger.warning('[AgentRuntime] MCP tools unavailable for user %s',
                               self.user_id, exc_info=True)
            else:
                self._mcp_descriptors = resolved
                descriptors.extend(resolved)
        return descriptors

    async def dispatch(self, name: str, args: dict[str, Any],
                       context: dict[str, Any]) -> str:
        """Run a tool, refusing anything the grants do not cover.

        Checked here and not only at advertising time: the model can name a tool
        it was never offered, and "we didn't mention it" is not access control.
        """
        from mcp_integration.tool_provider import is_mcp_tool

        if is_mcp_tool(name):
            if not self.mcp_allowed:
                return _denied(name, 'MCP tools')
            allowed, refusal = self.mcp_call_allowed(name)
            if not allowed:
                return _denied(name, refusal)
            from mcp_integration.tool_provider import MCPToolProvider
            return await MCPToolProvider.execute(name, args, self.user_id)

        if name in ('execute_python', 'run_python_on_files') and not self.grants.get('codeExecution'):
            return _denied(name, 'code execution')

        if self.tool_permissions.get(name) == 'deny':
            # Named refusal rather than the grant-shaped `_denied` below: the
            # grant *is* held and the screen said so, so "was not granted"
            # would send the owner to flip a switch that changes nothing.
            return (
                f"Error: '{name}' is not permitted for this agent — its "
                f"per-tool permission is 'deny'. Do not try it again; solve "
                f"the task with the tools you have, or say what you would need."
            )

        if name not in self.allowed_names:
            return _denied(name, name)

        permitted, refusal = await self.native_call_allowed(name)
        if not permitted:
            return _denied(name, refusal)

        # Simulated last, after every check a real call passes: grants, the
        # per-tool deny, the withheld set and the connector scope. Only the
        # live-card check is waived (inside `native_call_allowed`) — the call
        # names fixtures, never the real service.
        if self.environment is not None and self.environment.simulates(name):
            return await self.environment.run_simulated(name, args, context)

        from chat.tools import execute_tool
        return await execute_tool(name, args, context)


async def build_file_scope(agent, user, *, workspace=()):
    """The virtual-filesystem scope for this run, or None.

    Two settings have to agree before an agent touches files: a grant that
    reaches files (`fileOps`, `office`, or `codeExecution` for the file
    bridge) and `sandbox['fileAccess']` (which files). This resolves the
    second. It is async because a `scoped` agent's home folder is created on
    demand, which is a write.

    A failure here degrades the run to no file access rather than killing it —
    the same rule MCP follows a few lines below. An agent that cannot reach its
    folder should say so, not 500.
    """
    grants = agent.tool_grants or {}
    if not any(grants.get(g) for g in ('fileOps', 'office', 'media', 'codeExecution')):
        return None

    file_access = (agent.sandbox or {}).get('fileAccess', 'scoped')
    try:
        from inference.vfs import build_scope

        from inference.vfs import with_shared_workspace

        scope = await sync_to_async(build_scope)(
            user, file_access, agent_name=agent.name or '',
        )
        # A delegated worker also gets write access to the folder of whoever
        # asked, so it can hand back a path instead of pouring its whole answer
        # into the parent's context window. Adds only, and only where it can
        # mean something — see `with_shared_workspace`.
        if workspace:
            scope = with_shared_workspace(scope, workspace)
        return scope
    except Exception:
        logger.warning('[AgentRuntime] Could not build a file scope for agent %s',
                       agent.id, exc_info=True)
        return None


# `mcp_scope_for` lived here and returned a tuple of connection ids. It is now
# `agents/connector_scope.py`, which answers the same question one level finer —
# which *tools* of each connection — because picking a mailbox used to grant
# sending and deleting along with reading. The module keeps the two properties
# this function documented: an empty selection is unrestricted, and a stale or
# switched-off connection can only ever take tools away, since the scope is
# intersected with what the user can actually see.


def tool_scope_for(agent) -> tuple[str, ...]:
    """The built-in tools this agent may use, or `()` for all of its grants'.

    Only built-ins: an MCP tool's name is minted at runtime by a third party,
    so a stored list of them would narrow silently when one is renamed — which
    is the reason `connector_scope` answers that question per connection
    instead.
    """
    raw = (agent.agent_context or {}).get('toolScope') or []
    return tuple(sorted({str(t).strip() for t in raw if str(t).strip()}))


#: Per-tool permission modes for `agent_context['toolPermissions']`.
TOOL_PERMISSION_MODES = ('allow', 'ask', 'deny')


def tool_permissions_for(agent) -> dict[str, str]:
    """This agent's per-tool allow/ask/deny map, or `{}`.

    Built-ins only: MCP and native connector names are minted at runtime, so
    a stored one would rot the way `tool_scope_for`'s docstring describes,
    and `connector_scope` already answers that question. `ALWAYS_AVAILABLE`
    and `RETRIEVAL_TOOLS` are refused for the reason `tool_scope` ignores
    them — the plan, the clock and the archive are infrastructure, not
    capabilities.

    Defensive rather than validated here: the serializer rejects unknown
    names and modes on save, but rows predate every field and the runtime
    must never 500 on a stale one. Anything unrecognised is dropped, which
    can only ever leave today's behaviour in place.
    """
    grantable = {name for names in GRANT_TOOLS.values() for name in names}
    raw = (agent.agent_context or {}).get('toolPermissions') or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name).strip(): str(mode).strip().lower()
        for name, mode in raw.items()
        if str(name).strip() in grantable
        and str(mode).strip().lower() in TOOL_PERMISSION_MODES
    }


def merge_tool_permissions(parent: dict[str, str],
                           worker: dict[str, str]) -> dict[str, str]:
    """Most-restrictive-wins merge of a delegating run's map with its worker's.

    A worker may narrow what its parent allowed, never widen it: `deny`
    beats `ask` beats `allow`/unset. Without this a parent denied
    `delete_file` could delegate to a worker allowed it and get the deletion
    by proxy — the same hole `delegation_scope_for` closed for whole agents.
    """
    rank = {'allow': 0, 'ask': 1, 'deny': 2}
    merged = dict(worker)
    for name, mode in parent.items():
        if rank.get(mode, 0) > rank.get(merged.get(name, 'allow'), 0):
            merged[name] = mode
    return merged


def browser_domains_for(agent) -> tuple[str, ...]:
    """Sites this agent's `browser_act` may act on. Empty: it may only read."""
    raw = (agent.agent_context or {}).get('browserDomains') or []
    return tuple(sorted({str(d).lower().strip().strip('.') for d in raw if str(d).strip()}))


def browser_logins_for(agent) -> tuple[str, ...]:
    """Vault logins this agent's `browser_act` may fill. Empty: none."""
    raw = (agent.agent_context or {}).get('browserLogins') or []
    return tuple(sorted({str(s).strip() for s in raw if str(s).strip()}))


def recipients_for(agent) -> tuple[str, ...] | None:
    """Who this agent's unattended runs may message. None means no allowlist
    was written — and an unattended send with none is refused, so there is no
    way to inherit messaging reach by forgetting to configure it."""
    raw = (agent.agent_context or {}).get('recipients')
    if raw is None:
        return None
    return tuple(sorted({str(r).strip() for r in raw if str(r).strip()}))


def data_connections_for(agent) -> tuple[int, ...] | None:
    """Which databases this agent's runs may touch, or None for any the user
    owns. Empty means none — the field arrives with the feature."""
    raw = (agent.agent_context or {}).get('dataConnections')
    if raw is None:
        return None
    return tuple(sorted({
        v for v in raw if isinstance(v, int) and not isinstance(v, bool)}))


def api_connections_for(agent) -> dict[int, str] | None:
    """Which HTTP APIs this agent's runs may reach, as {id: read|all}, or
    None for any the user owns. Empty means none. Entries may be bare ids
    (full mode) or {"id", "mode": "read"} — the connector-scope shape."""
    raw = (agent.agent_context or {}).get('apiConnections')
    if raw is None:
        return None
    out: dict[int, str] = {}
    for entry in raw or []:
        if isinstance(entry, dict):
            try:
                key = int(entry.get('id'))
            except (TypeError, ValueError):
                continue
            out[key] = 'read' if str(entry.get('mode') or '').lower() == 'read' else 'all'
        else:
            try:
                out[int(entry)] = 'all'
            except (TypeError, ValueError):
                continue
    return out


def _host_scope(agent, key: str) -> tuple[str, ...]:
    """Extra hosts a scope allowlist adds. Empty: connections' own hosts."""
    raw = (agent.agent_context or {}).get(key) or []
    return tuple(sorted({str(h).lower().strip().strip('.')
                         for h in raw if str(h).strip()}))


def workspace_egress_for(agent) -> tuple[str, ...]:
    """Extra hosts the workspace may reach. Empty: the default egress."""
    return _host_scope(agent, 'workspaceEgress')


def code_projects_for(agent) -> tuple[int, ...] | None:
    """Which code projects this agent's runs may touch, or None for any the
    user owns. Empty means none — the field arrives with the feature."""
    raw = (agent.agent_context or {}).get('codeProjects')
    if raw is None:
        return None
    return tuple(sorted({
        v for v in raw if isinstance(v, int) and not isinstance(v, bool)}))


def delegation_scope_for(agent) -> tuple[int, ...] | None:
    """Which agents this one may delegate to, or None for any the user owns.

    The third of the grant/scope pairs, and the last one to get its second
    half: `subAgents` said *whether* an agent may delegate and nothing said *to
    whom*, so a delegating agent could reach every agent on the account —
    including ones holding grants it was refused, which turns delegation into a
    way around its own toolbox.

    Empty is unrestricted, for the same reason it is in `kb_scope_for` and
    `connector_scope`: the field arrives before enforcement does, and an agent
    that never chose must not be silently cut off from delegating at all.
    Non-integers are skipped rather than rejected — a stale id can only take a
    candidate away, since the tools still filter by owner.
    """
    raw = (agent.agent_context or {}).get('delegatesTo') or []
    ids = tuple(sorted({
        v for v in raw if isinstance(v, int) and not isinstance(v, bool)
    }))
    return ids or None


def write_paths_for(agent) -> tuple[str, ...] | None:
    """Glob list (relative to the project root) this agent may write, or None.

    None means unrestricted — the field arrives after the agents that predate
    it, and enforcing it must not silently strip every existing agent's writes.
    Normalised (leading `/` stripped, `..` clamped) at check time, not here.
    An explicitly empty intersection downstream is *not* None: it means the
    parent and the worker restricted disjointly, so the worker may write
    nothing — and the write check reads it that way.
    """
    raw = (agent.agent_context or {}).get('writePaths') or []
    if not isinstance(raw, list):
        return None
    cleaned = tuple(sorted({str(p).strip() for p in raw if str(p).strip()}))
    return cleaned or None


def command_scope_for(agent) -> tuple[str, ...] | None:
    """Command classes this agent's `ws_run` may reach, or None for any.

    None means unrestricted, for the same reason as `write_paths_for`: agents
    predate the field. Unknown classes are dropped here (the serializer refuses
    them on save); a stale row can only ever run wider than intended if the
    check trusted it, so the check treats unknown as absent.
    """
    raw = (agent.agent_context or {}).get('commandScope') or []
    if not isinstance(raw, list):
        return None
    cleaned = tuple(sorted({
        str(c).strip().lower() for c in raw
        if str(c).strip().lower() in CODE_COMMAND_CLASSES
    }))
    return cleaned or None


def intersect_write_paths(parent: tuple[str, ...] | None,
                          worker: tuple[str, ...] | None,
                          ) -> tuple[str, ...] | None:
    """Most-restrictive-wins for `writePaths`, glob-aware.

    None means unrestricted, so the intersection of "unrestricted" with
    anything is that thing. Two restricted lists intersect by subsumption:
    each side keeps the patterns the other side already covers
    (`workspaces.leases.subsumes`), so a lead on `src/**` fielding a worker
    on `src/api/**` yields `src/api/**` — the narrower — rather than nothing.
    Disjoint restrictions yield `()`, which the write check reads as "may
    write nothing", never as unrestricted.
    """
    from workspaces.leases import subsumes

    if parent is None:
        return worker
    if worker is None:
        return parent
    kept: set[str] = set()
    for w in worker:
        if any(subsumes(p, w) for p in parent):
            kept.add(w)
    for p in parent:
        if any(subsumes(w, p) for w in worker):
            kept.add(p)
    return tuple(sorted(kept))


def intersect_command_scope(parent: tuple[str, ...] | None,
                            worker: tuple[str, ...] | None,
                            ) -> tuple[str, ...] | None:
    """Most-restrictive-wins for `commandScope`, same None-means-unrestricted
    rule as `intersect_write_paths`. `any` on either side means that side
    imposes no class limit; the other side still narrows. Classes are a closed
    set, so plain set intersection is exact here.
    """
    if parent is None:
        return worker
    if worker is None:
        return parent
    if 'any' in parent:
        return worker
    if 'any' in worker:
        return parent
    return tuple(sorted(set(parent) & set(worker)))


def playbooks_for(agent) -> list[str]:
    """Playbook slugs this agent carries, in order, de-duplicated."""
    from agents.playbooks import PLAYBOOK_SLUGS

    raw = (agent.agent_context or {}).get('playbooks') or []
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        slug = str(entry or '').strip()
        if slug in PLAYBOOK_SLUGS and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _denied(name: str, capability: str) -> str:
    return (
        f"Error: '{name}' is not available to this agent — {capability} was not "
        f"granted. Do not try it again; solve the task with the tools you have, "
        f"or say what you would need."
    )


def approval_policy_for(autonomy: str):
    """Which per-call gate applies, for the calls `sensitive_tools_for` cannot name.

    MCP tool names are minted at runtime, so the name list above can never
    contain them — and an agent run is where that matters most, because nobody
    wrote the message that produced the call and nobody is watching the answer.
    `chat.permissions.unattended_policy` therefore gates every credentialed MCP
    call here, including reads that chat lets through on the strength of a human
    being present.

    `auto` is where that exemption comes back, and only there: it is the level
    that means "stop asking about things I can undo", so a credentialed *read*
    goes through while a credentialed write still stops. `plan` needs no policy
    at all because the toolbox has already withheld everything that could
    mutate — a gate over a set of pure reads would only ever say no to nothing.

    `full` opts out entirely: the user said no interruptions, and a gate they
    did not ask for would be this module deciding it knows better than the
    setting it was given.
    """
    from chat.tools import permissions

    if autonomy in ('full', 'plan'):
        return permissions.never
    if autonomy == 'auto':
        return permissions.default_policy
    return permissions.unattended_policy


def sensitive_tools_for(autonomy: str, toolbox: AgentToolbox) -> frozenset[str]:
    """Which tool calls pause for a human, per the agent's autonomy setting.

    `review` pausing on *everything* is what the word has to mean — a review
    setting that quietly exempted some calls would be the permissions screen
    lying again, just in the other direction.

    `auto` gates on the tools' own `effect="irreversible"` rather than on
    `sensitive`, because those answer different questions. `write_file` is
    sensitive and yet recoverable — a delete goes through `recycle.trash` into
    the user's own recycle bin — so it is exactly what this level exists to
    stop asking about. An unregistered name (every MCP tool) reports as
    irreversible, so the looser level never gets looser by accident.
    """
    if autonomy in ('full', 'plan'):
        return frozenset()
    if autonomy == 'review':
        return toolbox.allowed_names | {'execute_python'}
    if autonomy == 'auto':
        from chat.tools import names_with_effect
        return names_with_effect('irreversible')
    # 'ask': the calls with side effects outside our own walls.
    from chat.tools import SENSITIVE_TOOLS
    return frozenset(SENSITIVE_TOOLS) | {'execute_python'}


def switchable_modes(toolbox: AgentToolbox) -> dict:
    """Every level a running agent can be switched to, resolved against its toolbox.

    Handed to `TurnContext.approval_modes` so `tools_node` can apply a mid-run
    change without knowing what an agent or a grant is. Resolved once, here,
    because `review` means "every tool this agent has" — a question only the
    toolbox can answer, and one whose answer does not change during the run.

    `plan` is absent by construction: it is enforced by withholding tools when
    the toolbox is built, and the toolbox is already inside the frozen
    `TurnContext` by the time anyone could ask to switch. See
    `chat.turn.steering.SWITCHABLE`.
    """
    from chat.turn.steering import SWITCHABLE

    return {
        level: (sensitive_tools_for(level, toolbox), approval_policy_for(level))
        for level in SWITCHABLE
    }


# ── Who is asking ────────────────────────────────────────────────────────────
#
# The callers themselves (`CALLERS`, `UNATTENDED_CALLERS`) are declared in
# `agents/grants.py`; what happens once a caller is identified is below.


class AgentTurnFailed(RuntimeError):
    """The agent graph raised mid-run. Recorded as `failed`, never `completed`."""


class UnattendedNotPermitted(AgentRunRefused):
    """A trigger tried to run an agent that was never cleared to run alone."""


def _check_unattended(agent, caller: str) -> None:
    """A run with nobody watching has to have been asked for, once, explicitly.

    `allow_unattended` defaults to False and no migrated row sets it. That is
    deliberate: a trigger is a way for something other than the user — a clock,
    or an inbound HTTP request — to spend their model credits, and inheriting
    that permission from "the agent exists" is how a webhook becomes a bill.
    """
    if caller in UNATTENDED_CALLERS and not agent.allow_unattended:
        raise UnattendedNotPermitted(
            f'"{agent.name}" is not enabled for unattended runs. Turn on '
            f'"allow unattended" in its settings if you want a trigger or '
            f'another agent to be able to run it.'
        )


#: Agent statuses nothing but the owner may run. `archived` is `paused` plus
#: filed away: an archived agent firing on a schedule would be the surprise.
#: `draft` is deliberately absent. See `_check_status`.
STOPPED_STATUSES = frozenset({'paused', 'archived'})


class AgentPaused(AgentRunRefused):
    """Something other than the user tried to run an agent the user paused."""


def _check_status(agent, caller: str) -> None:
    """`paused` means nothing runs this agent but the user.

    The builder has promised "schedules stop firing and no agent may delegate to
    it" since the status became writable, and nothing enforced it: the sweep and
    the delegation tools both ran a paused agent exactly as an active one. The
    user can still run it by hand — pausing is a statement about automation,
    and refusing its owner would make "paused" the same as "deleted".
    """
    status = getattr(agent, 'status', '')
    if caller in UNATTENDED_CALLERS and status in STOPPED_STATUSES:
        raise AgentPaused(
            f'"{agent.name}" is {status}. Set it back to active in its settings '
            f'for schedules and other agents to run it.'
        )


async def resolve_agent_model(agent, user) -> tuple[str, str]:
    """Which provider/model this run actually uses.

    A blank on the agent means "the account default" (Settings ->
    `UserProfile.llm_provider` / `llm_model`), which is what the builder shows
    for a blank board and what `create_agent` promises when the model is left
    out. Only when the profile also says nothing does this fall back to the
    shipped `openrouter` + blank (the handler's own default). Resolved here —
    the one place every run passes — so the picker, the preflight and the turn
    cannot disagree about what a blank means.
    """
    provider = (agent.llm_provider or '').strip()
    model = (agent.llm_model or '').strip()
    if provider and model:
        return provider, model
    try:
        from core.models import UserProfile

        profile = await sync_to_async(
            lambda: UserProfile.objects.filter(user_id=user.id)
            .only('llm_provider', 'llm_model').first()
        )()
    except Exception:  # noqa: BLE001
        logger.warning('[AgentRuntime] Could not read user model default', exc_info=True)
        profile = None
    if not provider:
        provider = (profile.llm_provider.strip()
                    if profile and profile.llm_provider else '') or 'openrouter'
    if not model:
        model = (profile.llm_model.strip()
                 if profile and profile.llm_model else '') or ''
    return provider, model


async def _resolve_run_model(agent, user) -> tuple[str, str, str]:
    """(provider, model, fallback_from) this run will actually use.

    `fallback_from` is the configured model value when `llm.fallback` had to
    substitute the platform fallback (retired or unknown id), else ''. The
    agent row is never rewritten here — the config stays what the owner
    chose; the run record says what the run did about it.
    """
    provider, model = await resolve_agent_model(agent, user)
    old_model = (model or '').strip()
    try:
        from llm import fallback as _fallback

        provider, model, substituted, reason = await sync_to_async(
            _fallback.resolve_with_fallback)(provider, model)
    except Exception:  # noqa: BLE001
        logger.warning('[AgentRuntime] Fallback resolve failed', exc_info=True)
        return provider, model, ''
    if substituted:
        logger.info('[AgentRuntime] Agent %s: %s; using fallback %s/%s',
                    getattr(agent, 'id', '?'), reason, provider, model)
        return provider, model, old_model
    return provider, model, ''


def _record_model_fallback_notice(*, agent_id, agent_name, user_id,
                                  old_value, new_provider, new_model,
                                  execution_id) -> bool:
    """Write-once notice that a run fell back, plus the owner's notification.

    Returns True when this call notified. Never raises: telling the owner
    must not fail the run that already has its answer.
    """
    try:
        from django.contrib.auth import get_user_model

        from llm.models import ModelFallbackNotice
        from notifications.utils import create_notification

        _, created = ModelFallbackNotice.objects.get_or_create(
            subagent_id=agent_id, old_value=old_value)
        if not created:
            return False
        owner = get_user_model().objects.filter(pk=user_id).first()
        if owner is None:
            return False
        new_pair = f'{new_provider}/{new_model}'
        create_notification(
            owner, 'system',
            f'"{agent_name}" ran on the fallback model',
            f'Configured model `{old_value}` is retired or unknown, so this '
            f'run executed on the platform fallback `{new_pair}` instead. '
            "The agent's configuration was not changed — pick a replacement "
            'in the builder when ready.',
            data={
                'kind': 'model_fallback',
                'agent_id': agent_id,
                'execution_id': str(execution_id or ''),
                'old': old_value,
                'new': new_pair,
                'action_url': f'/agents/{agent_id}',
            },
            send_email=False,
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning('[AgentRuntime] Fallback notice failed for agent %s',
                       agent_id, exc_info=True)
        return False


# ── The brief ────────────────────────────────────────────────────────────────

@sync_to_async
def _gather_context(agent, user) -> dict[str, Any]:
    """Read the rows the agent's brief refers to, re-scoped to the owner.

    Re-filtering on `user` here rather than trusting the stored ids is the same
    guard `agents.py` applies on write: ownership can be revoked between saving
    an agent and running it.
    """
    from inference.models import KnowledgeBase
    from skills.models import Skill

    ctx = agent.agent_context or {}
    skills = list(
        Skill.objects.filter(user=user, id__in=ctx.get('skills') or [])
        .values_list('title', 'content')
    )
    # Ids and backends, not just names. The name alone was all the prompt ever
    # carried, so an agent had to spend a turn on `list_knowledge_bases`
    # rediscovering ids the configuration already knew — and the backend decides
    # which search tool even works, so a model told only the name would reach
    # for semantic search on a keyword-only KB and get nothing.
    kbs = list(
        KnowledgeBase.objects.filter(user=user, id__in=ctx.get('knowledgeBases') or [])
        .values('id', 'name', 'backend', 'doc_count')
        .order_by('name')
    )
    # Standing facts about the owner. Gathered here with everything else the
    # brief refers to, and read-only: `remember_about_user` is a chat tool, so
    # a scheduled run can be personalised by what the user has told the
    # assistant without being able to change it while nobody is watching.
    try:
        from core.memory import for_prompt

        user_memory = for_prompt(getattr(user, 'id', None))
    except Exception:  # noqa: BLE001
        logger.warning('[AgentRuntime] Could not read user memory', exc_info=True)
        user_memory = ''

    # The owner's Settings: timezone, language, name, bio. Read here for the
    # same reason as the memory above — a scheduled run has no conversation to
    # infer the person from — and read-only for the same reason too.
    from core.preferences import for_user as preferences_for

    return {'skills': skills, 'knowledge_bases': kbs, 'ctx': ctx,
            'user_memory': user_memory,
            'preferences': preferences_for(getattr(user, 'id', None))}


def kb_scope_for(gathered: dict[str, Any]) -> tuple[int, ...] | None:
    """Which knowledge bases this run may touch, or None for "any of the user's".

    None rather than "all of them" is load-bearing twice over. It is what an
    agent built before this setting was enforced keeps — its selection was only
    ever decorative, and turning enforcement on must not silently empty its
    corpus. And it is what chat passes, which is why the KB tools treat a
    missing scope as unrestricted rather than as a scope of nothing.
    """
    ids = tuple(kb['id'] for kb in gathered['knowledge_bases'])
    return ids or None


#: What to call for each KB backend. Stated in the prompt because the choice is
#: not guessable from the name and a wrong guess returns nothing rather than an
#: error — a semantic search against a keyword-only index is simply empty, which
#: a model reads as "the KB has nothing on this".
_KB_SEARCH_ADVICE = {
    'vector': 'semantic: knowledge_base_search',
    'fulltext': 'exact terms: keyword_search',
    'hybrid': 'semantic: knowledge_base_search, or exact terms: keyword_search',
    'raw': 'not indexed: list_documents then read_document',
}


def build_system_prompt(agent, gathered: dict[str, Any], file_scope: Any = None,
                        *, briefing: str = '', user_memory: str = '') -> str:
    """Assemble the agent's standing instructions.

    Guardrails are stated to the model as well as enforced in code. Enforcement
    is what makes them true; telling the model is what stops it burning turns
    planning around a tool it will never be handed.
    """
    guards = agent.guardrails or {}
    grants = agent.tool_grants or {}
    parts: list[str] = [
        f'You are {agent.name}, an autonomous agent.',
        '',
        'YOUR BRIEF',
        (agent.prompt or '').strip() or '(No brief was given. Ask what is wanted.)',
    ]

    if gathered['skills']:
        parts += ['', 'SKILLS — instructions you have been given for this work:']
        for title, content in gathered['skills']:
            parts.append(f'\n## {title}\n{content}')

    if gathered['knowledge_bases']:
        # id, backend and size, not just the name. The id is what the search
        # tools take, so listing names alone forced a `list_knowledge_bases`
        # turn to learn what this line already had in hand; the backend is what
        # decides which tool can read it at all.
        parts += ['', 'KNOWLEDGE BASES you can search (and only these):']
        for kb in gathered['knowledge_bases']:
            how = _KB_SEARCH_ADVICE.get(kb['backend'], _KB_SEARCH_ADVICE['vector'])
            parts.append(
                f"- id {kb['id']} · {kb['name']} · {kb['doc_count']} document(s) · {how}"
            )

    prefs = gathered.get('preferences')
    if gathered['ctx'].get('useEnvironment'):
        # In the owner's zone, named. The builder has always promised "time and
        # place"; this was a bare UTC ISO stamp, so an agent asked to act "this
        # morning" or "within business hours" reasoned in the wrong zone.
        from core.preferences import DEFAULTS, local_now

        where = prefs or DEFAULTS
        parts += ['', f'The current time for the user is {local_now(where)}.']

    if prefs is not None:
        from core.preferences import about_user

        about = about_user(prefs)
        if about:
            parts += ['', about]

    if briefing:
        # Shared background from the agent that delegated this run, sent once to
        # every worker in the fan-out. It is here rather than glued onto each
        # task because a task is copied into one window and paid for there,
        # while the background is the same for all of them — six workers with
        # the briefing restated in each task pay for it six times.
        #
        # Stated as *given* context, not as instruction: the goal is still the
        # task. A worker that treats its briefing as the job does the wrong one.
        parts += [
            '',
            'BRIEFING — background from the agent that delegated this to you. '
            'Context, not instructions; your task is stated separately.',
            briefing.strip(),
        ]

    if user_memory:
        # The same standing facts chat gets, for the same reason: an agent that
        # runs on a schedule has no conversation to infer the person from, so
        # without this it produces the same output for every user it is
        # installed by. Read-only here — an unattended run must not quietly
        # rewrite what the platform believes about someone, so the memory tools
        # are chat's alone.
        parts += ['', user_memory]

    # Playbooks are static text, so they belong in the session-stable system
    # prompt — the same bar the clock failed. Templates travel without ids, so
    # these ship as code rather than as `Skill` rows.
    for slug, body in __import__('agents.playbooks', fromlist=['load_many']).load_many(
        playbooks_for(agent)
    ):
        parts += ['', f'PLAYBOOK — {slug}:', body]

    # Said to every agent, because an agent run is the case a plan is for: it
    # can go 40 iterations, its transcript gets curated, and the instruction it
    # started from is the first thing curation folds away. The list is the only
    # state that survives that (`chat/turn/todos.py`), which is worth one line
    # of prompt on runs long enough to need it and costs a short run nothing —
    # the model is told plainly not to bother for simple work.
    parts += [
        '',
        'WORKING METHOD',
        '- For a task with several steps, call update_todos with your plan '
        'before you start, and keep it current as you go. Your open steps are '
        'shown back to you every turn — that is how you stay on track after a '
        'long stretch of tool calls. Skip it for simple work.',
        '- Never mark a step done that you did not do. If you cannot finish '
        'one, mark it blocked and say why; finishing with honest blockers is '
        'a better answer than a plan that claims false completion.',
        # The runtime has dispatched a turn's safe calls in parallel since the
        # batching passes landed (`tools_node`, `chat/tests/test_parallel_tools.py`),
        # but nothing ever asked the model to *use* that. Parallelism only helps
        # within one batch: a model that calls one tool, waits, then calls the
        # next gets none of it, and on a run that may go 40 iterations each
        # avoidable turn is a whole model round trip. Said here as well as in
        # chat's `CORE_RULES` because an agent run shares none of that prompt.
        '- When you need several things that do not depend on one another, ask '
        'for them in the same turn rather than one at a time: calls issued '
        'together are run in parallel, while one call per turn costs a full '
        'round trip each. Chain them only when one truly needs another\'s '
        'result.',
    ]

    granted = sorted(k for k, v in grants.items() if v and k not in UNSERVED_GRANTS)
    parts += [
        '',
        'LIMITS',
        f"- Capabilities granted: {', '.join(granted) or 'none beyond answering directly'}.",
        '- Any other tool will be refused. Do not retry a refused tool.',
        # Static, so it may sit in the cached prefix. Without it a model asks
        # in prose, which ends the run, or guesses silently.
        '- If the task is ambiguous in a way that changes what you do, call '
        'ask_user with the question and the assumption you will proceed on. '
        'Nobody answers during the run, so do not wait: carry on with the '
        'assumption and repeat the question in your final answer.',
    ]
    if guards.get('autonomy') != 'full':
        parts.append('- Some actions pause for human approval before they run.')
    # Unconditional as of 2026-09-03. This used to be gated on
    # `guardrails['egress']`, a three-value knob whose other two values the
    # sandbox could never have honoured: the production engine is a sidecar
    # container on an internal-only Docker network. Saying it always is the
    # true statement, and the knob is gone from the wire.
    parts.append('- Your sandbox has no network access.')
    # Stated up front rather than discovered by refusal. It only tells the model
    # something it could not infer when the readable and writable subtrees
    # differ — with `read_all_write_own`, every folder it lists is readable and
    # almost none are writable, and a model that learns that from a failed write
    # has already planned around the wrong shape.
    if file_scope is not None:
        if not file_scope.writable:
            parts.append('- Your file access is read-only. You can read files, '
                         'but not create, change or delete them.')
        elif file_scope.write_label != file_scope.label:
            parts.append(
                f'- You can read files anywhere under {file_scope.label}, but you '
                f'can only write, create and delete inside {file_scope.write_label}. '
                f'Save anything you produce there.'
            )
        else:
            parts.append(f'- Your files live under {file_scope.label}. You can '
                         f'read and write there.')
        # The second writable subtree, and the reason it exists. Stated as a
        # *preference*, not just a permission: a worker that can write here and
        # is not told to will still return its whole report in the transcript,
        # which is the cost the shared folder exists to avoid.
        if getattr(file_scope, 'shared_prefix', None):
            parts.append(
                f'- You are working for another agent, and you share its '
                f'folder {file_scope.shared_label}. When your answer is long — '
                f'a report, a dataset, a list of findings — write it there and '
                f'reply with the path and a two-line summary instead of the '
                f'whole thing. Whoever asked can read the file. Keep short '
                f'answers in your reply as normal.'
            )
    unserved = sorted(g for g in UNSERVED_GRANTS if grants.get(g))
    if unserved:
        parts.append(
            f"- {', '.join(unserved)} was configured but is not available in this "
            f'environment. Work without it and say so if it blocks you.'
        )
    # An output contract is what lets a *configured* agent stand in for a
    # hardcoded tool: the UI renders the shape, not the words. See
    # agents/contracts.py.
    from agents import contracts

    return '\n'.join(parts) + contracts.instruction_for(
        contracts.resolve(getattr(agent, 'output_schema', None))
    )


# ── Guardrails checked before the first token ────────────────────────────────

@sync_to_async
def _spend_this_month(agent, user) -> int:
    """What this agent has cost its owner so far this month, in rupees.

    Derived from what each run recorded — `cost_usd` where the run was priced,
    the blended `tokens_used` rate where it was not — never from `credits_used`,
    a column nothing writes, which returned zero on every run and left the cap
    below unable to refuse anything however low it was set. The conversion lives
    in `agents.spend` because `views/agents.py` has to show the user the same
    number this refuses them on.
    """
    from logs.models import ExecutionLog

    from agents.spend import aggregate_rupees

    start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return aggregate_rupees(
        ExecutionLog.objects
        .filter(user=user, subagent=agent, created_at__gte=start)
        .exclude(caller='eval')
    )


async def check_guardrails(agent, user) -> None:
    """Refuse the run if a guardrail already rules it out.

    The spend cap is checked before the run, not after: a cap enforced only on
    completion has already let the money go.
    """
    from django.utils import timezone as _tz

    try:
        from core.models import UserProfile

        profile = await UserProfile.objects.filter(user=user).afirst()
        paused = getattr(profile, 'paused_until', None) if profile else None
        if paused and paused > _tz.now():
            raise AgentRunRefused(
                'All runs are paused for this account until '
                f'{paused.astimezone().strftime("%H:%M %Z")}. '
                'Resume them from Settings to run anything.'
            )
    except AgentRunRefused:
        raise
    except Exception:  # noqa: BLE001 — a profile read must not stop a run
        logger.exception('[Agent] Could not read pause state')
    cap = (agent.guardrails or {}).get('spendCapRupees')
    if cap:
        spent = await _spend_this_month(agent, user)
        if spent >= cap:
            raise AgentRunRefused(
                f'This agent has reached its monthly spend cap ({spent}/{cap}). '
                f'Raise the cap in its settings to keep running it.'
            )


# ── Run ──────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class AgentRun:
    """What one agent run produced."""

    execution_id: str
    answer: str
    thinking: str
    tool_trace: list[dict[str, Any]]
    tokens: int
    awaiting_approval: bool
    unserved_grants: tuple[str, ...]
    duration_ms: int
    #: Present when the agent was configured with an output contract and met
    #: it. None means either no contract or a contract it failed.
    structured: dict[str, Any] | None = None
    contract_error: str = ''
    #: Approvals the run would have paused for and questions it asked — see
    #: `collect_intents`. Approvals appear only on eval runs, which record
    #: instead of pausing.
    intents: list[dict[str, Any]] = field(default_factory=list)


@sync_to_async
def _open_log(agent, user, goal: str, trigger_type: str, thread_id: str = '',
              *, caller: str = 'api', depth: int = 0,
              parent_step_id: int | None = None, delegation_task: str = '',
              delegation_index: int = 0, model_used: str = '',
              fallback_from: str = ''):
    from logs.models import ExecutionLog
    from logs import revisions

    # The configuration this run will execute under, pinned at open time rather
    # than read back at close: an agent edited mid-run must not retroactively
    # change what its running executions claim to have used. `current()` mints
    # revision 1 for agents that predate revision tracking, so every run has
    # something to point at.
    revision = revisions.current(agent) if agent is not None else None

    return ExecutionLog.objects.create(
        subagent=agent,
        revision=revision,
        user=user,
        status='running',
        trigger_type=trigger_type,
        # `trigger_type` says how the run was invoked; `caller` says what
        # invoked it. Both a chat delegation and a direct API call arrive as
        # `trigger_type='api'`, and telling them apart is the difference
        # between "the user asked for this" and "an agent spent their credits".
        caller=caller,
        depth=depth,
        parent_step_id=parent_step_id,
        delegation_task=delegation_task,
        delegation_index=delegation_index,
        # `thread_id` is stored, not just the goal: it is the checkpointer key,
        # and resuming an approved run has to find the *same* log so the trace
        # stays one execution instead of splitting into two the canvas cannot
        # join back up.
        #
        # Written twice, deliberately. The column is what every lookup filters
        # on — it is indexed, where `input_data__thread_id` was a full scan
        # with a JSON parse per row on the two paths a person waits on
        # (approving a pause, and delegating). The JSON copy stays because it
        # is what the historical rows carry and what a run's own record shows.
        thread_id=(thread_id or '')[:200],
        input_data={'goal': goal, 'thread_id': thread_id},
        started_at=timezone.now(),
        # What actually served the run, when that differs from the config.
        # Set by the caller, which resolved the fallback before opening.
        model_used=(model_used or '')[:150],
        fallback_from=(fallback_from or '')[:150],
    )


@sync_to_async
def _close_log(log, *, status: str, result: dict[str, Any], tokens: int,
               error: str = '', extra_cost_usd=None,
               extra_cost_source: str = '', exc=None) -> None:
    from logs import failures as _failures

    # A finished run holds no locks: leases release on every terminal path
    # (completed, failed, cancelled, timeout) — deliberately not on `paused`,
    # where the run still owns its half-made change. Best-effort: freeing a
    # lock must never fail a run that already has its answer.
    try:
        from workspaces.leases import release_holder as _release_holder

        _release_holder(log.id)
    except Exception:  # noqa: BLE001
        logger.warning('[Leases] Release on close failed for run %s', log.id)
    try:
        from workspaces import reads as _reads

        _reads.discard_thread((log.input_data or {}).get('thread_id') or '')
    except Exception:  # noqa: BLE001
        pass

    log.status = status
    log.output_data = result
    log.tokens_used = tokens
    log.error_message = error
    try:
        log.failure_category = _failures.classify(status, error, exc)
    except Exception:  # noqa: BLE001
        log.failure_category = ''
    log.completed_at = timezone.now()
    if log.started_at:
        log.duration_ms = int(
            (log.completed_at - log.started_at).total_seconds() * 1000
        )
    _roll_up_cost(log, extra_cost_usd=extra_cost_usd,
                  extra_cost_source=extra_cost_source)
    if not tokens:
        # The cancel, timeout and failure paths close with `tokens=0` because
        # they have no final state to read it from — but the turns that did run
        # were paid for. Zero here made those runs free to the spend cap, which
        # counts tokens for any run without a price on record.
        from django.db.models import Sum

        from logs.models import AgentTurn

        log.tokens_used = (
            AgentTurn.objects.filter(execution=log)
            .aggregate(total=Sum('tokens'))['total'] or 0
        )
    log.save(update_fields=[
        'status', 'output_data', 'tokens_used', 'error_message',
        'failure_category',
        'completed_at', 'duration_ms', 'updated_at',
        'input_tokens', 'output_tokens', 'cached_read_tokens',
        'cached_write_tokens', 'cost_usd', 'cost_source',
    ])
    try:
        from logs.signals_api import record_signal as _record

        if status in ('failed', 'timeout'):
            _record(log.user_id, 'failed',
                    execution_id=str(log.execution_id),
                    detail={'category': log.failure_category or ''})
        elif status == 'cancelled':
            _record(log.user_id, 'cancelled',
                    execution_id=str(log.execution_id), detail={})
    except Exception:  # noqa: BLE001
        pass


#: Tools that spend money themselves, beside the model calls — their results
#: carry `cost_usd` / `cost_source` (`chat/tools/media.py`).
COSTED_TOOLS = ('generate_image',)


def _tool_costs(log) -> tuple[Decimal, list[str]]:
    """What this run's own tool calls cost, read back from their step rows.

    From the rows rather than counted in memory, for the reason the turn
    rollup gives: a resumed run starts with a fresh process but the same rows,
    and a step re-run on resume updates its row instead of adding one. No new
    column — the tool writes its cost into the result the step already keeps.
    """
    import json as _json

    from logs.models import AgentStep

    total, sources = Decimal('0'), []
    for result in (AgentStep.objects
                   .filter(execution=log, tool__in=COSTED_TOOLS, status='completed')
                   .values_list('result', flat=True)):
        raw = (result or {}).get('result')
        try:
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get('cost_usd'):
            total += Decimal(str(parsed['cost_usd']))
            sources.append(str(parsed.get('cost_source') or 'estimated'))
    return total, sources


def _roll_up_cost(log, *, extra_cost_usd=None,
                  extra_cost_source: str = '') -> None:
    """Sum this run's turns onto the run, in place.

    `extra_cost_usd` is spend that belongs to the run but not to any turn —
    today that means the context-curation fold, which is a real model call with
    no place in the turn numbering. Passed in rather than queried because there
    is no row to query: it exists only on the stream that observed it.

    Summed from `AgentTurn` rather than accumulated in memory because the turn
    rows are the record: a resumed run re-enters the loop with a fresh
    in-process total but the same turn rows, and `update_or_create` on
    `(execution, index)` means re-running a turn corrects its row instead of
    adding a second one. Anything counted in memory would double on resume.

    A failure here costs the cost figures, never the run: the answer is already
    in hand and `log.save` is about to record it.
    """
    from django.db.models import Sum
    from llm.pricing import combine_sources
    from logs.models import AgentTurn

    try:
        turns = AgentTurn.objects.filter(execution=log)
        totals = turns.aggregate(
            input=Sum('input_tokens'), output=Sum('output_tokens'),
            cached_read=Sum('cached_read_tokens'),
            cached_write=Sum('cached_write_tokens'),
            cost=Sum('cost_usd'),
        )
        log.input_tokens = totals['input'] or 0
        log.output_tokens = totals['output'] or 0
        log.cached_read_tokens = totals['cached_read'] or 0
        log.cached_write_tokens = totals['cached_write'] or 0
        tool_cost, tool_sources = _tool_costs(log)
        log.cost_usd = (totals['cost'] or Decimal('0')) + tool_cost + (
            Decimal(str(extra_cost_usd)) if extra_cost_usd else Decimal('0')
        )
        # The total is only as trustworthy as its least-known turn, so one
        # unpriced turn makes the run unpriced. A confident sum that silently
        # omits a turn is worse than an admitted gap.
        log.cost_source = combine_sources(
            list(turns.values_list('cost_source', flat=True))
            + tool_sources
            + ([extra_cost_source] if extra_cost_source else [])
        )
    except Exception:  # noqa: BLE001
        logger.exception('[Agent] Failed to roll up cost for %s', log.execution_id)


@sync_to_async
def _cancel_pending_hitl(log) -> int:
    """Withdraw the approvals a cancelled run will now never act on.

    A `HITLRequest` left `pending` after its run is gone is not merely stale:
    `notifications/reminders.py` sweeps on `status='pending'`, so it keeps
    escalating and lands in the daily digest, asking the user to approve a step
    that no longer exists.
    """
    from agents.models import HITLRequest

    return HITLRequest.objects.filter(execution=log, status='pending').update(
        status='cancelled', responded_at=timezone.now(),
    )


async def _finalise_cancelled(log, stream, started: float) -> None:
    """Close out a run whose task was cancelled.

    Shielded because this runs *inside* the `CancelledError` handler: a bare
    await here is itself a cancellation point, and the first ORM round-trip
    would re-raise before the log was ever written — which is the exact bug
    this function exists to fix. `chat/turn/runs.py::stop` solves the same
    problem the other way round, by finalising from the canceller rather than
    the cancellee; a run can also be cancelled by shutdown or by a parent task,
    where there is no canceller to do it, so this path has to stand alone.
    """
    async def _work() -> None:
        await _cancel_pending_hitl(log)
        # Cancelled is as final as failed — nothing will resume this thread.
        # The key lives in `input_data`, which is where `_open_log` puts it.
        thread_id = (log.input_data or {}).get('thread_id')
        if thread_id:
            from chat.turn.agent import forget_thread
            await forget_thread(thread_id)
        await _close_log(log, status='cancelled', result={}, tokens=0,
                         error='Run cancelled.')
        await stream.run_finished(
            status='cancelled', answer='Run cancelled.',
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    try:
        await asyncio.shield(_work())
    except asyncio.CancelledError:
        # Shield lets `_work` finish even though the await was interrupted.
        raise
    except Exception:  # noqa: BLE001
        logger.exception('[AgentRuntime] Could not finalise cancelled run')


def _live_task(execution_id: str) -> asyncio.Task | None:
    """The task running this execution in *this* process, if there is one.

    Found by name: `start_agent_run` and `resume_agent_run` spawn with
    `agent-run:<id>` / `agent-resume:<id>`, so no registry has to be kept in
    step with the task's life.
    """
    names = {f'agent-run:{execution_id}', f'agent-resume:{execution_id}'}
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:
        return None
    return next((t for t in tasks if t.get_name() in names and not t.done()), None)


class CannotCancel(Exception):
    """The run cannot be stopped from here; the message says why."""


#: How long a stop waits for the run to finish unwinding before answering.
CANCEL_WAIT_SECONDS = 5.0


async def cancel_agent_run(log) -> str:
    """Stop a run. Returns the status it ends in.

    Three shapes of "in flight", each stopped differently:

    - **A task in this process** is cancelled. The run's own `CancelledError`
      branch closes the log (`_finalise_cancelled`), so there is one closing
      path however a run is cancelled — by this, a parent, or shutdown.
    - **Paused for approval** has no task at all: `interrupt()` returned and
      the checkpoint is all that is left. So the row is closed here, its
      approval withdrawn, and its checkpoint dropped — the same three things
      the cancel branch does, and the reason Deny alone was not enough.
    - **A delegated worker** runs inside its parent's task, so it cannot be
      stopped alone without the parent waiting on a result that never comes.
      Refused, naming the parent to stop instead.

    A `running` row with no task here is either on another worker process or
    orphaned; recovery (`agents/recovery.py`) closes orphans, and this says so
    rather than writing `cancelled` over a run that is still going elsewhere.
    """
    execution_id = str(log.execution_id)
    if log.status not in ('running', 'pending', 'paused'):
        return log.status

    task = _live_task(execution_id)
    if task is not None:
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), CANCEL_WAIT_SECONDS)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        await sync_to_async(log.refresh_from_db)()
        return log.status

    if log.parent_step_id:
        raise CannotCancel(
            'This run was started by another agent and runs inside it. Stop '
            'the run that delegated it.'
        )

    if log.status == 'paused':
        await _cancel_pending_hitl(log)
        thread_id = (log.input_data or {}).get('thread_id')
        if thread_id:
            from chat.turn.agent import forget_thread
            await forget_thread(thread_id)
        await _close_log(log, status='cancelled', result={}, tokens=0,
                         error='Run cancelled while waiting for approval.')
        return 'cancelled'

    raise CannotCancel(
        'This run is not running in this server process. If it has stopped '
        'responding it will be closed automatically.'
    )


async def _apply_run_fallback(log, agent, user, provider: str, model: str,
                            fallback_from: str, sink) -> None:
    """Record and announce a fallback substitution. Best-effort throughout.

    The run already has its model pair; this writes `model_used` /
    `fallback_from` onto the log (unless the opener already did), tells the
    watching caller via STATUS, and notifies the owner exactly once per
    (agent, retired model) through `ModelFallbackNotice`.
    """
    try:
        from chat.turn.events import Event

        if ((log.fallback_from or '') != fallback_from
                or (log.model_used or '') != (model or '')):
            log.model_used = (model or '')[:150]
            log.fallback_from = (fallback_from or '')[:150]
            await sync_to_async(log.save)(
                update_fields=['model_used', 'fallback_from', 'updated_at'])
        if sink is not None:
            try:
                await sink(Event.STATUS, {
                    'phase': 'model_fallback',
                    'message': (
                        f'Configured model `{fallback_from}` is retired or '
                        f'unknown — running on the platform fallback '
                        f'`{provider}/{model}` instead.'),
                })
            except Exception:  # noqa: BLE001 — telling must not fail the run
                pass
        await sync_to_async(_record_model_fallback_notice)(
            agent_id=agent.id, agent_name=agent.name, user_id=user.id,
            old_value=fallback_from, new_provider=provider, new_model=model,
            execution_id=log.execution_id)
    except Exception:  # noqa: BLE001
        logger.warning('[AgentRuntime] Fallback bookkeeping failed',
                       exc_info=True)


async def run_agent(agent, goal: str, *, user, sink=None,
                    thread_id: str | None = None,
                    trigger_type: str = 'manual', caller: str = 'api',
                    depth: int = 0, log=None,
                    parent_step_id: int | None = None,
                    delegation_task: str = '',
                    delegation_index: int = 0,
                    deadline=None,
                    briefing: str = '',
                    parent_session_key: str = '',
                     workspace=(),
                     task_claims=(), task_id: str = '',
                     worker_label: str = '',
                     write_paths=None, command_scope=None,
                     gated_calls: str = 'run',
                     environment: Any = None) -> AgentRun:
    """Run `agent` against `goal` and record the run.

    `gated_calls` applies to `caller='eval'` only: a call that would pause is
    recorded as an intent and then `run` for real or `block`ed (declined).

    `environment` is an `eval/environment.py::EvalEnvironment` (duck-typed —
    this module must not import `eval`). When set, the run is confined to
    the eval world: the file scope is rooted at the attempt folder whatever
    the agent's `fileAccess` says, the KB scope is the world's hidden KB,
    tools with no simulator are withheld, and simulated calls are answered
    from fixtures. Only `caller='eval'` passes one.

    `thread_id` keys the checkpointer, so passing the id of a paused run is how
    an approved run resumes — the same mechanism chat uses, reached through
    `chat.agent.approve_tool_call`.

    `log` lets a caller open the `ExecutionLog` first and hand it in, which is
    how `start_agent_run` can return an execution id before the run has done
    anything. Left None, this opens its own.

    `deadline` is an `agents.budget.Deadline` and is how a *worker* is bounded
    by the run that asked for it: `invoke_subagent` passes down a share of its
    own remaining time, so a subagent configured for an hour inside a parent
    with four minutes left gets four minutes. Left None, the agent's own
    configured limit applies — which is what a top-level run wants.
    """
    from llm import access as llm
    from chat.turn.agent import TurnContext, iteration_limit, run_turn
    from chat.turn.curation import CurationPolicy
    from chat.turn.events import null_sink

    from agents import budget, connector_scope
    from .stream import AgentRunStream, tee

    # Checked here even when `start_agent_run` already checked it. The cost is
    # one aggregate query; the alternative is a flag that skips a spend limit,
    # and a safety check with an off switch is not a safety check. The same
    # reasoning covers the unattended gate: schedules and triggers reach this
    # function without passing through any view.
    _check_unattended(agent, caller)
    _check_status(agent, caller)
    await check_guardrails(agent, user)

    started = time.monotonic()
    # A run inherits its parent's clock or starts its own; it never gets both,
    # and it can never extend one it was handed. `Deadline` is frozen for that
    # reason — a worker that could re-derive its own limit from its saved row
    # would make the parent's bound advisory.
    deadline = deadline if deadline is not None else budget.Deadline.for_agent(agent)
    thread_id = thread_id or f'agent-{agent.id}-{uuid.uuid4()}'
    if log is None:
        log = await _open_log(
            agent, user, goal, trigger_type, thread_id,
            caller=caller, depth=depth, parent_step_id=parent_step_id,
            delegation_task=delegation_task, delegation_index=delegation_index,
        )

    # Every run streams to the execution channel, whether or not the caller
    # asked for a sink: that is what makes the run visible on the workflow
    # canvas, and a run nobody watched still has to be replayed afterwards.
    stream = AgentRunStream(log)
    await stream.run_started(goal)

    # Resolved here — the one place every run passes — so the picker, the
    # preflight and the turn cannot disagree. A retired or unknown model id
    # is substituted with the platform fallback up front (recorded on the log
    # and announced, never written back to the agent row).
    provider, model, fallback_from = await _resolve_run_model(agent, user)

    try:
        # Repeated from `start_agent_run` for the callers that arrive here
        # directly — a schedule, a trigger, a resumed run. It costs one
        # credential lookup, and it is what turns "the agent failed" into "this
        # provider has no credential", which is the only version of the message
        # the user can act on. Inside the try so it closes the log and tells the
        # channel by the same path every other failure takes.
        await llm.preflight(provider=provider, model=model, user_id=user.id)
        if fallback_from:
            await _apply_run_fallback(
                log, agent, user, provider, model, fallback_from, sink)

        guards = agent.guardrails or {}
        autonomy = guards.get('autonomy', 'ask')
        if environment is not None:
            # Prepared by the caller (`eval/runner.py`): the attempt folder
            # and the simulators already exist. Rooted here, the walk itself
            # is the confinement — the agent's own `fileAccess` is not
            # consulted, because "read everything" must not survive into a
            # run whose whole point is that the owner is not in the room.
            file_scope = environment.attempt_scope()
        else:
            file_scope = await build_file_scope(agent, user, workspace=workspace)
        # `plan` is the one level that changes which tools exist rather than
        # which ones pause, so it has to be known before the toolbox is built.
        toolbox = AgentToolbox.for_agent(
            agent, user.id, file_scope=file_scope,
            read_only=(autonomy == 'plan'),
            session_key=thread_id,
            archive_scopes=(parent_session_key,) if parent_session_key else (),
            environment=environment,
        )
        gathered = await _gather_context(agent, user)
        if environment is not None:
            # The prompt names the world's hidden KB, never the owner's
            # rows — naming them would send the run to ask for ids it is
            # then refused.
            gathered = environment.filter_gathered(gathered)
            world_kb = environment.kb_scope()
            # No KB surface: an id that matches nothing, so even a rag tool
            # reached by some path finds no corpus. The tools are withheld
            # too (`withheld_names`); this is the second door, not the first.
            env_kb_scope = world_kb if world_kb is not None else (-1,)
        else:
            env_kb_scope = None

        turn = TurnContext(
            provider=provider,
            model=model,
            system_message=build_system_prompt(
                agent, gathered, file_scope, briefing=briefing,
                user_memory=gathered.get('user_memory', ''),
            ),
            user_id=user.id,
            session_id=thread_id,
            # The chat agent widens its iteration budget for research-shaped
            # work. An agent run is that shape by definition — it was given a
            # goal, not a question — so it gets the same headroom.
            intent='research',
            user_text=goal,
            memory_enabled=False,
            # The builder's slider, finally connected. Default 0.2 matches what
            # `AgentSerializer` has always declared and shown the user; until
            # `TurnContext.temperature` existed the value was stored and never
            # read, so every agent ran at the library default of 0.7 instead.
            temperature=float((agent.runtime_settings or {}).get('temperature', 0.2)),
            # The builder's effort choice. Blank means the model's own default,
            # so an agent saved before the knob existed runs exactly as it did.
            # Not clamped here: `llm.access` is the only place that knows which
            # rungs this model offers, and it snaps rather than refuses.
            effort=(agent.runtime_settings or {}).get('effort') or None,
            max_iterations=iteration_limit('research'),
            sink=tee(sink or null_sink, stream.sink),
            tool_source=toolbox.descriptors,
            tool_dispatch=toolbox.dispatch,
            sensitive_tools=sensitive_tools_for(autonomy, toolbox),
            approval_policy=approval_policy_for(autonomy),
            # What a mid-run switch would mean, resolved up front. Without it
            # `tools_node` ignores the override entirely, which is what chat
            # wants and what any caller that has not opted in gets.
            approval_modes=switchable_modes(toolbox),
            # Per-tool allow/ask/deny, folded into the approval gate by
            # `tools_node` after the level above is resolved. Empty for chat
            # and for every agent saved before the field existed.
            tool_permissions=tool_permissions_for(agent),
            on_tool_result=stream.on_tool_result,
            # One `AgentTurn` row per model call. Without it every tool call in
            # the run is unattributed, and the reasoning behind the run is lost
            # when the process ends.
            on_model_turn=stream.on_model_turn,
            # The three context-lifecycle toggles, finally connected. Same
            # history as `temperature` above: stored, validated, round-tripped
            # to the builder and read by nothing, so a long run's only defence
            # was `clamp_input` dropping its oldest segments at the wire with no
            # record of what went. A run whose transcript never reaches the high
            # mark never notices this exists.
            curation=CurationPolicy.from_settings(agent.runtime_settings),
            on_curation=stream.on_curation,
            # This run has an `ExecutionLog`, so `stream._approval_requested`
            # can queue a paused call as a `HITLRequest` and the reminder ladder
            # takes it from there. Telling the graph stops it writing a second,
            # unconditional notification of its own — see `TurnContext`.
              approval_queue=True,
              # An eval has nobody to answer a pause, so a gated call is
              # recorded as an intent and then runs (see `TurnContext`).
              record_intents=(gated_calls if caller == 'eval' else ''),
              # Someone can answer an `ask_user` question unless the run was
              # started by a schedule or trigger: the chat that started it,
              # the manager that delegated it, or the person at the Run button.
              can_ask=caller not in ('trigger', 'eval'),
              # A worker is one level deeper than whoever asked for it, and the
              # counter is what stops delegation multiplying without bound.
              depth=depth,
              # What started the run. Tools that are safe watched but not
              # unwatched read it (`publish_page` above `link` refuses an
              # unattended caller).
              caller=caller,
            # The soft stop. `agent_node` withholds tools once this is
            # `wrapping_up`, which turns "out of time" into the same last pass
            # that running out of iterations already produced: an answer built
            # from what the run has, rather than a kill mid-tool-call that
            # returns nothing having paid for everything.
            deadline=deadline,
            # Which slice of the user's document tree the file tools address.
            # None when file access is off, which is also when the toolbox has
            # already withheld the tools.
            file_scope=file_scope,
            # And which of the user's agents this one may hand work to. Same
            # shape and same default as the two scopes below it.
            delegation_scope=delegation_scope_for(agent),
            # Where `browser_act` may act. Always a tuple for an agent run.
            browser_domains=browser_domains_for(agent),
            # Which vault logins it may fill. Empty means none: the field
            # arrives with the feature, so no existing agent holds one.
            browser_logins=browser_logins_for(agent),
            # Who its unattended runs may message. None means no allowlist.
            recipients=recipients_for(agent),
            # Which databases and APIs its runs may reach. None means any
            # the user owns; empty means none.
            data_connections=data_connections_for(agent),
            api_connections=api_connections_for(agent),
            db_hosts=_host_scope(agent, 'dbHosts'),
            api_hosts=_host_scope(agent, 'apiHosts'),
            # Extra hosts the workspace may reach. Empty: the default egress.
            workspace_egress=workspace_egress_for(agent),
            # Which code projects `shell` tools may touch. None: any owned.
            code_projects=code_projects_for(agent),
            # And which knowledge bases the KB tools may reach. The builder's
            # selection, finally enforced rather than merely printed into the
            # prompt: before this an agent configured for one KB could search
            # every other KB its owner had. Inside an eval world it is the
            # world's hidden KB, or an id matching nothing when the world
            # has no KB surface.
            kb_scope=(env_kb_scope if env_kb_scope is not None
                      else kb_scope_for(gathered)),
            # A worker may read what its parent archived, and nothing else.
            # Empty for every run a person started.
            archive_scopes=(parent_session_key,) if parent_session_key else (),
            # Coding-team scopes (C1/C2): the agent's own limits, narrowed by
            # the caller (parent → worker most-restrictive-wins, then the
            # task's claims at dispatch). Explicit overrides win; otherwise the
            # row's own selection stands.
            write_paths=(tuple(write_paths) if write_paths is not None
                         else write_paths_for(agent)),
            command_scope=(tuple(command_scope) if command_scope is not None
                           else command_scope_for(agent)),
            task_claims=tuple(task_claims or ()),
            task_id=task_id or '',
            worker_label=worker_label or '',
            execution_id=str(log.execution_id),
        )

        # The backstop under the soft stop. The loop checks the clock between
        # passes, which cannot help against a single call that never returns —
        # a provider holding a socket open, an MCP subprocess that hangs. The
        # grace is what separates the two: reaching *this* means the soft stop
        # was given its chance and something ignored it.
        async with asyncio.timeout(
            deadline.remaining() + budget.RUN_WRAPUP_SECONDS
        ) as clock:
            result = await run_turn(turn, prompt=goal, thread_id=thread_id)
        if result.error:
            # `run_turn` turns a crash into an apology for chat's sake. A run
            # is a record, so it goes down the failure path below instead of
            # being closed as `completed` with the apology as its answer.
            raise AgentTurnFailed(result.error)
    except TimeoutError:
        if not clock.expired():
            # Somebody else's TimeoutError — a socket, a subprocess — that
            # happened to surface here. Reporting it as "your agent ran out of
            # time" would send the owner to raise a limit that was never the
            # problem, so it goes down the ordinary failure path below.
            raise
        # Distinct from cancelled on purpose: `timeout` is a limit this system
        # imposed and the owner can raise, while `cancelled` is a person having
        # pressed stop. Reporting either as the other sends whoever reads the
        # run to the wrong place. `ExecutionLog` has carried a `timeout` status
        # since it was written; until now nothing wrote it.
        logger.warning('[AgentRuntime] Agent %s exceeded its %ss limit',
                       agent.id, deadline.limit)
        message = budget.describe(deadline)
        from chat.turn.agent import forget_thread
        await forget_thread(thread_id)
        await _cancel_pending_hitl(log)
        await _close_log(log, status='timeout', result={}, tokens=0,
                         error=message)
        await stream.run_finished(
            status='timeout', answer=message,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise AgentRunRefused(message) from None
    except asyncio.CancelledError:
        # Not reachable from `except Exception`: since 3.8 `CancelledError` is a
        # `BaseException`. Without this branch a cancelled run left its
        # `ExecutionLog` at `running` for ever, its HITL rows nudging the user
        # about a step that no longer existed, and the canvas showing a spinner
        # with nothing behind it.
        logger.info('[AgentRuntime] Agent %s cancelled', agent.id)
        await _finalise_cancelled(log, stream, started)
        raise
    except Exception as exc:
        logger.exception('[AgentRuntime] Agent %s failed', agent.id)
        from chat.turn.agent import forget_thread
        await forget_thread(thread_id)
        # The catalogue said the model was live but the provider answered
        # 410/404 anyway (stale catalogue between refreshes). The run fails
        # visibly — mid-run model switches would silently change provenance —
        # but the owner is still told once, through the same notice as a
        # pre-resolved fallback, so the fix is one builder visit away.
        try:
            if isinstance(exc, llm.LLMModelUnavailable) and agent is not None:
                from llm import fallback as _fallback_mod

                fb_provider, fb_model = await sync_to_async(
                    _fallback_mod.get_fallback)()
                if (provider, model) != (fb_provider, fb_model):
                    await sync_to_async(_record_model_fallback_notice)(
                        agent_id=agent.id, agent_name=agent.name,
                        user_id=user.id, old_value=model,
                        new_provider=fb_provider, new_model=fb_model,
                        execution_id=log.execution_id)
        except Exception:  # noqa: BLE001
            logger.warning('[AgentRuntime] Fallback race notice failed',
                           exc_info=True)
        await _close_log(log, status='failed', result={}, tokens=0, error=str(exc))
        await stream.run_finished(
            status='failed', answer=str(exc),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise

    # A steer posted just as the run finished has nobody left to read it;
    # dropping the slot keeps the mailbox from accumulating one entry per run.
    from chat.turn import steering
    if not result.awaiting_approval:
        steering.discard(thread_id)

    status = 'paused' if result.awaiting_approval else 'completed'
    if status != 'paused':
        # The run is over and this thread id can never be reached again — agent
        # threads are `agent-<id>-<uuid>` and workers get a throwaway. The
        # checkpointer keeps every super-step for the life of the process
        # otherwise, which on a small box is the whole memory budget after
        # enough runs. A *paused* run keeps its checkpoint: that is what the
        # approval resumes from.
        from chat.turn.agent import forget_thread
        await forget_thread(thread_id)
    structured, contract_error = _apply_contract(agent, result)
    payload: dict[str, Any] = {
        'answer': result.answer, 'tool_trace': result.tool_trace,
    }
    # What the run produced for a person to *look at*, not just read. Carried
    # on `output_data` because that is what the run view reads: a chart the
    # agent drew and the plan it worked to exist only in the turn's metadata
    # otherwise, which nothing outside the graph can reach once the run ends.
    if charts := (result.metadata or {}).get('charts'):
        payload['charts'] = charts
    if todos := (result.metadata or {}).get('todos'):
        payload['todos'] = todos
    if files := (result.metadata or {}).get('files'):
        payload['files'] = files
    intents = collect_intents(result.metadata, result.tool_trace)
    if intents:
        payload['intents'] = intents
    # The lead's tasks, final state each — the panel redraws from this on
    # `/runs` after the fact, the way it redraws todos and charts. Keyed by
    # the run's own thread id, which is the bucket the dispatch tools filed
    # under. Read, not popped: thread ids are unique per run, and a worker
    # still going when its lead closes stays stoppable by handle until the
    # TTL prunes it.
    try:
        from agents.agent import tasks as _code_tasks

        bucket = _code_tasks._tasks.get(thread_id)
        if bucket:
            payload['tasks'] = [_code_tasks.task_frame(t) for t in bucket.values()]
        _code_tasks.drop_sink(thread_id)
    except Exception:  # noqa: BLE001
        pass
    if structured is not None:
        payload['structured'] = structured
    if contract_error:
        payload['contract_error'] = contract_error
    if stream.curation['passes']:
        # Only when it actually happened. A `context_curation` key reading all
        # zeroes on every short run would make the interesting case harder to
        # spot, not easier.
        #
        # Copied and stringified: this lands in a JSONField, and `cost_usd` is
        # a Decimal, which `json.dumps` refuses. Stringified rather than
        # floated, for the reason money is a string everywhere else here.
        payload['context_curation'] = {
            **stream.curation,
            'cost_usd': format_usd(stream.curation['cost_usd']),
        }

    await _close_log(
        log,
        status=status,
        result=payload,
        tokens=result.tokens,
        error=contract_error,
        # Curation's own spend. Not an `AgentTurn` — a fold has no place in the
        # model's turn numbering — so it cannot be picked up by the rollup and
        # has to be handed in.
        extra_cost_usd=stream.curation['cost_usd'],
        extra_cost_source=stream.curation['cost_source'],
    )
    await stream.run_finished(
        status=status, answer=result.answer,
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    return AgentRun(
        execution_id=str(log.execution_id),
        answer=result.answer,
        thinking=result.thinking,
        tool_trace=result.tool_trace,
        tokens=result.tokens,
        awaiting_approval=result.awaiting_approval,
        unserved_grants=toolbox.unserved,
        duration_ms=int((time.monotonic() - started) * 1000),
        structured=structured,
        contract_error=contract_error,
        intents=intents,
    )


def collect_intents(metadata: dict | None, tool_trace: list | None) -> list[dict[str, Any]]:
    """What the run wanted from a person: approvals it would have paused
    for, and questions it asked through `ask_user`, in the order they arose.

    Approvals come from `metadata['intents']` (written only when the turn was
    told to record rather than pause). Questions come from the trace, because
    `ask_user` is an ordinary tool and its call *is* the record.
    """
    intents = [dict(i) for i in ((metadata or {}).get('intents') or [])]
    for call in tool_trace or []:
        if (call.get('tool') or call.get('name')) != 'ask_user':
            continue
        args = call.get('args') or {}
        intents.append({
            'kind': 'question',
            'question': str(args.get('question') or ''),
            'assumption': str(args.get('assumption') or ''),
            'call_id': call.get('call_id', ''),
            'iteration': call.get('iteration', 0),
        })
    intents.sort(key=lambda i: int(i.get('iteration') or 0))
    return intents


def _apply_contract(agent, result) -> tuple[dict[str, Any] | None, str]:
    """Check a finished run against the contract it was configured with.

    Returns `(structured, error)`. A contract failure is *reported*, not
    repaired: an agent set up to produce research output and returning prose
    has failed at the thing it was configured for, and quietly wrapping the
    prose would make every agent appear to satisfy every contract — which
    would make the whole mechanism decorative.
    """
    from agents import contracts

    contract = contracts.resolve(getattr(agent, 'output_schema', None))
    if contract is None or result.awaiting_approval:
        return None, ''

    try:
        return contracts.coerce(result.answer, contract), ''
    except contracts.ContractError as exc:
        logger.warning('[AgentRuntime] Agent %s broke its output contract: %s',
                       agent.id, exc)
        return None, str(exc)


async def start_agent_run(agent, goal: str, *, user,
                          thread_id: str | None = None,
                          trigger_type: str = 'manual',
                          caller: str = 'api',
                          parent_step_id: int | None = None,
                          delegation_task: str = '',
                          delegation_index: int = 0,
                          workspace: tuple[str, ...] = ()) -> str:
    """Begin a run in the background and return its execution id immediately.

    `workspace` is the caller's own write folder, granted to the run as a
    second writable subtree — the same hand-off `invoke_subagent` makes, so a
    run started by `run_agent` can answer with a path too.

    Two things have to happen before the caller gets a response, and both are
    the reason this exists rather than the view spawning `run_agent` directly:

    - **Guardrails and credentials are checked first.** A run that cannot be
      paid for — a spent cap, or a provider this user has no key for — is
      refused while the caller is still waiting, so it surfaces as a 402 rather
      than as a run that appears to start and dies silently in the background.
    - **The `ExecutionLog` is opened first**, so the returned execution id is
      already subscribable. A client that opens the socket the moment it gets
      the id must not miss the first step.

    Uses `spawn()` rather than `asyncio.create_task`: the run outlives its
    request, and a detached task inheriting the request's executor would raise
    `CurrentThreadExecutor already quit` on its first ORM call — see
    `workflow_backend/background.py`.
    """
    from llm import access as llm
    from workflow_backend.background import spawn

    from agents import admission
    from .stream import AgentRunStream

    _check_unattended(agent, caller)
    _check_status(agent, caller)
    await check_guardrails(agent, user)
    # A missing credential is the same class of problem as a spent budget: the
    # caller can fix it, but only if they are told. Raised here, before the log
    # exists, it reaches the view as an error naming the provider — instead of
    # a 202 followed by a run that dies on its first model call, where the only
    # trace is a failed execution the user has to go and open.
    provider, model, fallback_from = await _resolve_run_model(agent, user)
    await llm.preflight(
        provider=provider,
        model=model,
        user_id=user.id,
    )
    thread_id = thread_id or f'agent-{agent.id}-{uuid.uuid4()}'
    log = await _open_log(
        agent, user, goal, trigger_type, thread_id,
        caller=caller, parent_step_id=parent_step_id,
        delegation_task=delegation_task, delegation_index=delegation_index,
        model_used=model, fallback_from=fallback_from,
    )

    async def _run() -> None:
        try:
            # A slot, before any work. This is the only bound on how much of
            # the box one account may hold at once — the spend cap is monthly
            # and per agent, so twenty schedules firing together are twenty
            # runs it permits and one instance cannot serve. Taken *inside* the
            # spawned task rather than before it, so the caller still gets its
            # 202 and its execution id immediately: a queued run is visible and
            # subscribable, it just has not started yet.
            #
            # Only top-level runs come through here. Workers reach `run_agent`
            # directly from `invoke_subagent`, and must: a worker queueing
            # behind the parent that is awaiting it would deadlock.
            #
            # The queued frame first: until `run_agent` broadcasts
            # `workflow_start` on admission, a client subscribed on the 202
            # cannot tell a run that is waiting for a slot from one whose
            # task died silently. Best-effort — a run must not fail because
            # nobody could be told it is waiting.
            try:
                await AgentRunStream(log).run_queued()
            except Exception:  # noqa: BLE001
                logger.exception('[AgentRuntime] Could not announce queued run')
            async with admission.slot(user.id):
                await run_agent(agent, goal, user=user, thread_id=thread_id,
                                trigger_type=trigger_type, caller=caller,
                                log=log, workspace=workspace)
        except admission.AdmissionTimeout as exc:
            # Nothing ran, so there is nothing to report as having failed
            # part-way. The log is closed here because `run_agent` never got to
            # open it — leaving it at `running` would show the owner a spinner
            # for a run that was never admitted.
            logger.warning('[AgentRuntime] Agent %s not admitted: %s',
                           agent.id, exc)
            try:
                await _close_log(log, status='failed', result={}, tokens=0,
                                 error=str(exc))
                await AgentRunStream(log).run_finished(
                    status='failed', answer=str(exc), duration_ms=0,
                )
            except Exception:  # noqa: BLE001
                logger.exception('[AgentRuntime] Could not close unadmitted run')
        except Exception:
            # `run_agent` already closed the log as failed and told the
            # channel. Nothing is waiting on this task, so swallow rather than
            # leave an unretrieved exception on the loop.
            logger.exception('[AgentRuntime] Background run of agent %s failed',
                             agent.id)

    spawn(_run(), name=f'agent-run:{log.execution_id}')
    return str(log.execution_id)


async def start_agent_run_and_wait(agent, goal: str, *, user,
                                   trigger_type: str = 'schedule',
                                   caller: str = 'trigger') -> str:
    """Start a run and wait for its background task to finish (blocking).

    The sync-context twin of `start_agent_run`, for callers with no persistent
    event loop — the trigger sweep (`agents/sweep.py`), whether reached as the
    Celery beat task or as `manage.py run_due_triggers`.

    `start_agent_run` detaches its work with `background.spawn()`, which is
    correct on the ASGI loop that outlives the request and fatal anywhere else:
    wrapped in `async_to_sync` the spawn lands on a temporary loop that is
    closed the moment the start returns, so the sweep opened an `ExecutionLog`,
    re-armed the trigger as `fired`, and the agent never ran — with the orphan
    `running` row then holding every later firing `busy` behind the overlap
    policy. Awaiting the spawned task on this same loop runs it to completion
    instead. Pre-start refusals raise exactly as with `start_agent_run`;
    anything the run itself records stays on its `ExecutionLog`.
    """
    execution_id = await start_agent_run(
        agent, goal, user=user, trigger_type=trigger_type, caller=caller,
    )
    task = _live_task(execution_id)
    if task is not None:
        await task
    return execution_id


async def resume_agent_run(agent, *, user, thread_id: str) -> str | None:
    """Continue a run that paused for approval, on its original execution id.

    Approval on its own only records consent in the checkpoint — the paused run
    has already returned, so without this nothing would ever pick it back up
    and the user would approve into silence.

    Reuses the paused `ExecutionLog` rather than opening a new one: the canvas
    subscribes per execution, and a resumed half arriving on a second id would
    leave the trace split across two runs with no way to join them.
    """
    from workflow_backend.background import spawn

    log = await _find_paused_log(agent, thread_id)
    if log is None:
        logger.warning('[AgentRuntime] No paused run for thread %s', thread_id)
        return None

    goal = (log.input_data or {}).get('goal', '')
    await _reopen_log(log)

    async def _run() -> None:
        try:
            # `depth` is carried over from the log, not left to default to 0.
            # A worker that paused for approval would otherwise resume as
            # though the user had started it, and could delegate again past
            # MAX_DELEGATION_DEPTH — the counter is only a bound if it survives
            # a pause. `caller` stays 'api' on purpose: a human just approved
            # this, so the run is attended however it began, and re-deriving
            # `caller='trigger'` here would send it back through the unattended
            # gate that the pause has already made moot.
            await run_agent(agent, goal, user=user, thread_id=thread_id,
                            trigger_type=log.trigger_type or 'manual', log=log,
                            depth=log.depth or 0)
        except Exception:
            logger.exception('[AgentRuntime] Resume of agent %s failed', agent.id)

    spawn(_run(), name=f'agent-resume:{log.execution_id}')
    return str(log.execution_id)


@sync_to_async
def _find_paused_log(agent, thread_id: str):
    from logs.models import ExecutionLog

    # Indexed column, not the JSON path it mirrors: this runs on every
    # approval, and the JSON filter scanned every run the account had ever
    # made. Migration `logs.0018` backfilled the column, so a row that
    # predates it is still found.
    return ExecutionLog.objects.filter(
        subagent=agent, status='paused', thread_id=thread_id,
    ).order_by('-started_at').first()


@sync_to_async
def _reopen_log(log) -> None:
    log.status = 'running'
    log.completed_at = None
    log.save(update_fields=['status', 'completed_at', 'updated_at'])
