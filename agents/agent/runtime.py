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
from dataclasses import dataclass
from decimal import Decimal

from llm.pricing import format_usd
from typing import Any

from asgiref.sync import sync_to_async
from django.utils import timezone

# Pure data — no models, no Django app registry — so this is safe at module
# scope even though the rest of the agent layer is imported lazily.
from agents import connector_scope

logger = logging.getLogger(__name__)

#: Grant key -> the built-in tool names it unlocks. This map *is* the
#: permissions model; a tool absent from every value is unreachable by an agent
#: no matter what the model asks for.
GRANT_TOOLS: dict[str, tuple[str, ...]] = {
    'webSearch': ('web_search', 'deep_research', 'image_search', 'video_search'),
    'scrape': ('scrape_webpage', 'read_url'),
    # All five, not two. `list_knowledge_bases` tells the model to use
    # `keyword_search` on a keyword KB and `list_documents` + `read_document` on
    # a raw one — tools this grant did not unlock, so the catalogue was
    # instructing the agent to call things it would then be refused, and an
    # agent whose KB was keyword- or raw-backed could not read it at all.
    'rag': ('list_knowledge_bases', 'knowledge_base_search', 'keyword_search',
            'list_documents', 'read_document'),
    'codeExecution': ('execute_python',),
    # The virtual filesystem over the user's own Folder/Document tree
    # (`inference/vfs.py`). What the agent may actually do with these is a
    # second axis — `sandbox['fileAccess']` decides read-only vs. its own
    # folder vs. the whole tree, and `none` means the tools are never offered
    # even with the grant on. The grant says "may touch files at all"; the
    # scope says "which files".
    'fileOps': ('list_files', 'find_files', 'read_file', 'write_file',
                'make_directory', 'delete_file'),
    # Delegation. There is no 'orchestrator' kind of agent — an agent that
    # fans out to other agents is one holding this grant, so composition is
    # checked by the same mechanism as every other capability instead of by a
    # second code path that could disagree with it.
    'subAgents': ('invoke_subagent', 'search_agents'),
    # MCP tools are resolved per-user at runtime rather than named here, so the
    # grant unlocks the whole user-configured set. Their names are namespaced by
    # `mcp_integration.tool_provider`, which is what keeps them from colliding
    # with a built-in and slipping past the allow-list.
    'mcp': (),
}

#: Granted in the builder but with no implementation the runtime is willing to
#: serve. `shell` has no sandbox at all (AGENT_TEMPLATES.md §9.1), so the
#: runtime refuses and says so rather than pretending the grant was honoured.
#:
#: `fileOps` was here too, because the chat file tools that were deliberately
#: removed reached the *host* filesystem and re-adding them through an agent
#: grant would have undone that decision silently. It is served now because
#: what it unlocks is a different capability: `inference/vfs.py` addresses rows
#: in the user's own document tree and cannot name a path on any disk. The half
#: of that decision that is still true — no host filesystem from a chat turn —
#: is still pinned by `chat/tests/test_rework.py::RemovedCapabilityTests`.
UNSERVED_GRANTS = frozenset({'shell'})

#: Available whatever the grants say: no side effects, no egress, no reads of
#: anything the user owns.
#:
#: `update_todos` qualifies on all three counts — it writes the run's own plan
#: into the run's own state and touches nothing else. Putting it behind a grant
#: would mean an agent could be configured *not to be able to keep track of
#: what it was doing*, which is not a capability anyone would deliberately
#: withhold, and the tool matters most on exactly the long runs a cautious
#: grant set would be applied to.
ALWAYS_AVAILABLE = ('get_current_time', 'update_todos')

#: Offered only once this run has actually stored something — a tool result too
#: large to replay, or a step the curator removed. Both read back the run's own
#: transcript, so neither is a capability a grant should have to unlock: the
#: text was already shown to this agent, in this run, and the only question is
#: whether it can still see it.
#:
#: They were unreachable before curation existed, which made the notices that
#: name them dishonest — `tool_output.bound` has always told the model to "call
#: read_tool_output with that id", and no agent could, because the toolbox
#: filters `AVAILABLE_TOOLS` by the names its grants unlock and no grant named
#: it. An escape hatch nobody can open is worse than none, because the model
#: stops looking for another way out.
RETRIEVAL_TOOLS = ('read_tool_output', 'recall_context')


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

    @classmethod
    def for_agent(cls, agent, user_id: int, *, file_scope: Any = None,
                  read_only: bool = False, session_key: str = '',
                  archive_scopes: tuple[str, ...] = ()) -> AgentToolbox:
        grants = {k: bool(v) for k, v in (agent.tool_grants or {}).items()}
        unserved = tuple(sorted(g for g in UNSERVED_GRANTS if grants.get(g)))
        return cls(grants=grants, user_id=user_id, unserved=unserved,
                   file_scope=file_scope, read_only=read_only,
                   session_key=session_key, archive_scopes=archive_scopes,
                   mcp_scope=connector_scope.for_agent(agent))

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
        if self.read_only:
            from chat.tools import READ_ONLY_TOOLS
            names &= set(READ_ONLY_TOOLS)
        if self.file_scope is None:
            # Granted `fileOps` but `fileAccess='none'` — the two settings
            # disagree, and the safe reading is the narrower one. Withheld
            # rather than left to refuse at call time: an advertised tool that
            # cannot run is worse than one never offered, because the model
            # plans around it and then has to explain the failure.
            names -= set(GRANT_TOOLS['fileOps'])
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

        if name == 'execute_python' and not self.grants.get('codeExecution'):
            return _denied(name, 'code execution')

        if name not in self.allowed_names:
            return _denied(name, name)

        from chat.tools import execute_tool
        return await execute_tool(name, args, context)


async def build_file_scope(agent, user):
    """The virtual-filesystem scope for this run, or None.

    Two settings have to agree before an agent touches files: the `fileOps`
    grant (may it at all) and `sandbox['fileAccess']` (which files). This
    resolves the second. It is async because a `scoped` agent's home folder is
    created on demand, which is a write.

    A failure here degrades the run to no file access rather than killing it —
    the same rule MCP follows a few lines below. An agent that cannot reach its
    folder should say so, not 500.
    """
    if not (agent.tool_grants or {}).get('fileOps'):
        return None

    file_access = (agent.sandbox or {}).get('fileAccess', 'scoped')
    try:
        from inference.vfs import build_scope

        return await sync_to_async(build_scope)(
            user, file_access, agent_name=agent.name or '',
        )
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


def _denied(name: str, capability: str) -> str:
    return (
        f"Error: '{name}' is not available to this agent — {capability} was not "
        f"granted. Do not try it again; solve the task with the tools you have, "
        f"or say what you would need."
    )


#: The autonomy ladder, strictest first. Each level is a *pair* — the tool
#: names that pause on sight, and the policy that judges the calls no name list
#: could contain — because MCP names are minted at runtime and a level defined
#: by names alone would silently exempt exactly the tools holding the user's
#: real credentials.
#:
#: `plan` and `auto` are the two rungs that make the ladder usable rather than
#: merely present. Without `plan` the only way to learn what an agent will do
#: is to let it do it; without `auto` the choice is between approving recycled
#: file writes one at a time and approving nothing at all, and a user faced
#: with that picks `full` and stops reading the prompts — which is the failure
#: this whole module is trying to avoid.
AUTONOMY_LADDER: tuple[str, ...] = ('plan', 'review', 'ask', 'auto', 'full')


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

#: Every way a run can begin. The runtime has exactly one entry point and these
#: are its callers; they differ in configuration, never in code path. A second
#: way to start a run is a second place for the guardrail checks to be
#: forgotten.
CALLERS = frozenset({'chat', 'orchestrator', 'trigger', 'api'})

#: Callers where no human is present at the moment the run starts. `chat` and
#: `api` are both a person pressing something; `orchestrator` inherits the
#: attendedness of whatever started *it*, and is treated as unattended because
#: the safe answer is the one that asks more often, not less.
UNATTENDED_CALLERS = frozenset({'trigger', 'orchestrator'})


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

    return {'skills': skills, 'knowledge_bases': kbs, 'ctx': ctx,
            'user_memory': user_memory}


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

    if gathered['ctx'].get('useEnvironment'):
        parts += ['', f'The current time is {timezone.now().isoformat()}.']

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
    ]

    granted = sorted(k for k, v in grants.items() if v and k not in UNSERVED_GRANTS)
    parts += [
        '',
        'LIMITS',
        f"- Capabilities granted: {', '.join(granted) or 'none beyond answering directly'}.",
        '- Any other tool will be refused. Do not retry a refused tool.',
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
    )


async def check_guardrails(agent, user) -> None:
    """Refuse the run if a guardrail already rules it out.

    The spend cap is checked before the run, not after: a cap enforced only on
    completion has already let the money go.
    """
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


@sync_to_async
def _open_log(agent, user, goal: str, trigger_type: str, thread_id: str = '',
              *, caller: str = 'api', depth: int = 0,
              parent_step_id: int | None = None, delegation_task: str = '',
              delegation_index: int = 0):
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
        input_data={'goal': goal, 'thread_id': thread_id},
        started_at=timezone.now(),
    )


@sync_to_async
def _close_log(log, *, status: str, result: dict[str, Any], tokens: int,
               error: str = '', extra_cost_usd=None,
               extra_cost_source: str = '') -> None:
    log.status = status
    log.output_data = result
    log.tokens_used = tokens
    log.error_message = error
    log.completed_at = timezone.now()
    if log.started_at:
        log.duration_ms = int(
            (log.completed_at - log.started_at).total_seconds() * 1000
        )
    _roll_up_cost(log, extra_cost_usd=extra_cost_usd,
                  extra_cost_source=extra_cost_source)
    log.save(update_fields=[
        'status', 'output_data', 'tokens_used', 'error_message',
        'completed_at', 'duration_ms', 'updated_at',
        'input_tokens', 'output_tokens', 'cached_read_tokens',
        'cached_write_tokens', 'cost_usd', 'cost_source',
    ])


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
        log.cost_usd = (totals['cost'] or Decimal('0')) + (
            Decimal(str(extra_cost_usd)) if extra_cost_usd else Decimal('0')
        )
        # The total is only as trustworthy as its least-known turn, so one
        # unpriced turn makes the run unpriced. A confident sum that silently
        # omits a turn is worse than an admitted gap.
        log.cost_source = combine_sources(
            list(turns.values_list('cost_source', flat=True))
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


async def run_agent(agent, goal: str, *, user, sink=None,
                    thread_id: str | None = None,
                    trigger_type: str = 'manual', caller: str = 'api',
                    depth: int = 0, log=None,
                    parent_step_id: int | None = None,
                    delegation_task: str = '',
                    delegation_index: int = 0,
                    deadline=None,
                    briefing: str = '',
                    parent_session_key: str = '') -> AgentRun:
    """Run `agent` against `goal` and record the run.

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
    # canvas, and a run nobody watched still has to be replayable afterwards.
    stream = AgentRunStream(log)
    await stream.run_started(goal)

    provider = agent.llm_provider or 'openrouter'
    model = agent.llm_model or ''

    try:
        # Repeated from `start_agent_run` for the callers that arrive here
        # directly — a schedule, a trigger, a resumed run. It costs one
        # credential lookup, and it is what turns "the agent failed" into "this
        # provider has no credential", which is the only version of the message
        # the user can act on. Inside the try so it closes the log and tells the
        # channel by the same path every other failure takes.
        await llm.preflight(provider=provider, model=model, user_id=user.id)

        guards = agent.guardrails or {}
        autonomy = guards.get('autonomy', 'ask')
        file_scope = await build_file_scope(agent, user)
        # `plan` is the one level that changes which tools exist rather than
        # which ones pause, so it has to be known before the toolbox is built.
        toolbox = AgentToolbox.for_agent(
            agent, user.id, file_scope=file_scope,
            read_only=(autonomy == 'plan'),
            session_key=thread_id,
            archive_scopes=(parent_session_key,) if parent_session_key else (),
        )
        gathered = await _gather_context(agent, user)

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
            # A worker is one level deeper than whoever asked for it, and the
            # counter is what stops delegation multiplying without bound.
            depth=depth,
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
            # And which knowledge bases the KB tools may reach. The builder's
            # selection, finally enforced rather than merely printed into the
            # prompt: before this an agent configured for one KB could search
            # every other KB its owner had.
            kb_scope=kb_scope_for(gathered),
            # A worker may read what its parent archived, and nothing else.
            # Empty for every run a person started.
            archive_scopes=(parent_session_key,) if parent_session_key else (),
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
    )


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
                          delegation_index: int = 0) -> str:
    """Begin a run in the background and return its execution id immediately.

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
    await check_guardrails(agent, user)
    # A missing credential is the same class of problem as a spent budget: the
    # caller can fix it, but only if they are told. Raised here, before the log
    # exists, it reaches the view as an error naming the provider — instead of
    # a 202 followed by a run that dies on its first model call, where the only
    # trace is a failed execution the user has to go and open.
    await llm.preflight(
        provider=agent.llm_provider or 'openrouter',
        model=agent.llm_model or '',
        user_id=user.id,
    )
    thread_id = thread_id or f'agent-{agent.id}-{uuid.uuid4()}'
    log = await _open_log(
        agent, user, goal, trigger_type, thread_id,
        caller=caller, parent_step_id=parent_step_id,
        delegation_task=delegation_task, delegation_index=delegation_index,
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
            async with admission.slot(user.id):
                await run_agent(agent, goal, user=user, thread_id=thread_id,
                                trigger_type=trigger_type, caller=caller,
                                log=log)
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

    return ExecutionLog.objects.filter(
        subagent=agent, status='paused', input_data__thread_id=thread_id,
    ).order_by('-started_at').first()


@sync_to_async
def _reopen_log(log) -> None:
    log.status = 'running'
    log.completed_at = None
    log.save(update_fields=['status', 'completed_at', 'updated_at'])
