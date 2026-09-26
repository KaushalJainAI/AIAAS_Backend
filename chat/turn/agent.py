"""
The chat agent: a LangGraph tool loop over real chat messages.

The important property of this module — the one the previous implementation
lacked — is that the model sees a *proper transcript*. An assistant turn that
requested tools is sent back as an assistant message carrying `tool_calls`, and
each result as a `tool` message with the matching `tool_call_id`.

That is not a stylistic preference. Flattening tool results into prose ("---
PREVIOUS ACTIONS AND TOOL RESULTS ---") takes the model off the distribution it
was trained on and it starts *imitating* tool calls in text instead of emitting
them, which is why the old code needed a 20-pattern scraper to read them back.
Thread the messages correctly and native tool calls just work; the scraper in
`extraction.py` shrinks to a fallback for weak local models.

Read-only turn settings live in `TurnContext` on the runnable config, not in
graph state, so the checkpointer only ever persists what actually changes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Annotated, Any, Awaitable, Callable, Sequence, TypedDict
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from workflow_backend.background import release_db
from workflow_backend.thresholds import MAX_TOOL_ITERATIONS

from asgiref.sync import sync_to_async
from decimal import Decimal

from llm import access as llm
from llm.usage import EMPTY_USAGE, TokenUsage
from . import checkpoints, prompts, todos
from .events import Event, EventSink, null_sink
from llm.access import (
    Completion,
    LLMUnavailable,
    LLMUserActionable,
    StreamAccumulator,
    humanize_provider_body,
    ToolCall,
)

logger = logging.getLogger(__name__)

LLM_CALL_TIMEOUT = 180
TOOL_CALL_TIMEOUT = 120

#: Output room by intent. Long-form work needs more than a chat reply.
_MAX_TOKENS_BY_INTENT: dict[str, int] = {
    "coding": 16_384,
    "research": 16_384,
    "file_manipulation": 16_384,
}
_DEFAULT_MAX_TOKENS = 8_192

_NO_PROVIDER_MESSAGE = (
    "I can't reach a language model right now. Add and verify a provider "
    "credential (for example OpenRouter) in Settings, then try again."
)


# ── Turn configuration ───────────────────────────────────────────────────────

#: Returns the OpenAI-shaped tool descriptors the model may see this turn.
ToolSource = Callable[[], Awaitable[list[dict[str, Any]]]]
#: Runs one tool call: (name, arguments, context) -> result text.
ToolDispatch = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[str]]
#: Decides whether one call needs a human, beyond the `sensitive_tools` names:
#: (name, arguments, tool context) -> True to pause. It exists because MCP tool
#: names are minted at runtime from a third-party catalogue, so no static list
#: can hold them, and `credential_injector` hands them the user's real keys.
#: See `chat.permissions` for the rules and why reads are exempt in chat.
ApprovalPolicy = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[bool]]
#: Observes one finished tool call. Called after dispatch returns *or* raises,
#: so a caller recording a trace sees failures as well as successes. Takes the
#: keyword payload described on `TurnContext.on_tool_result`. Must not raise:
#: an observer that throws would fail a tool call that actually succeeded.
ToolObserver = Callable[..., Awaitable[None]]
#: Observes one finished model call — the *turn*, which is the unit an agent
#: actually reasons in. Called once per pass of `model_node`, after the
#: completion is in hand, with the keyword payload described on
#: `TurnContext.on_model_turn`. Must not raise, for the same reason
#: `ToolObserver` must not: watching a run may not break it.
TurnObserver = Callable[..., Awaitable[None]]

@dataclass(frozen=True, slots=True)
class TurnContext:
    """Read-only settings for one turn. Lives on the config, not in state."""

    provider: str
    model: str
    system_message: str
    user_id: int
    session_id: str
    intent: str
    user_text: str
    history: tuple[dict[str, Any], ...] = ()
    attachments: tuple[Any, ...] = ()
    memory_enabled: bool = True
    max_iterations: int = MAX_TOOL_ITERATIONS
    sink: EventSink = null_sink

    #: Sampling temperature for this turn's model calls. Defaults to `llm.stream`'s
    #: own default so chat behaves exactly as before; the agent runtime overrides it
    #: from `SubAgent.runtime_settings['temperature']`. Before this field existed the
    #: builder's temperature slider was stored, round-tripped to the UI, and then
    #: dropped on the floor — every agent turn ran at 0.7 whatever the user chose.
    temperature: float = 0.7

    #: How hard the model is asked to think, from `llm.effort.LADDER`, or None
    #: for the model's own default. Carried alongside `temperature` because it
    #: is the same kind of thing — a per-turn knob the caller chose — but it is
    #: deliberately *not* clamped here: what a given model offers is registry
    #: data, so `llm.access` snaps the level at the one point that knows both
    #: the request and the model. A level this model has no rung for costs
    #: nothing; it simply never reaches the wire.
    effort: str | None = None

    #: Identity for this turn, generated rather than passed: it exists only so a
    #: tool can meter itself per turn (`ask_vision` caps how many times it will
    #: interrogate one image before the user is paying for a loop). `session_id`
    #: cannot do that job — it spans every turn in the conversation — and
    #: `thread_id` is not visible from inside a tool.
    turn_id: str = field(default_factory=lambda: uuid4().hex)

    # Optional overrides that let a non-chat caller — the agent runtime —
    # own which tools exist, how they run, and which need approval. The
    # agent runtime must gate on a grant list the chat registry knows
    # nothing about, and a denied grant has to fail at call time, not just
    # go unadvertised. Left None, the chat defaults apply unchanged.
    tool_source: ToolSource | None = None
    tool_dispatch: ToolDispatch | None = None
    sensitive_tools: frozenset[str] | None = None

    #: Per-tool allow/ask/deny for this run (`{name: mode}`), from the agent's
    #: `agent_context['toolPermissions']` — merged most-restrictive-wins with
    #: the delegating parent's map for workers. Read in `tools_node`, which
    #: folds `ask` into the pause-on-sight set and `allow` out of it, *after*
    #: the mid-run autonomy override is resolved, so the overrides survive a
    #: switch. Empty for chat and for every agent saved before the field
    #: existed, where the autonomy ladder decides alone. `deny` never gates
    #: here: denied tools are withheld and refused by the toolbox instead.
    tool_permissions: dict[str, str] = field(default_factory=dict, repr=False,
                                             compare=False)

    #: Scratch space for work that is the same on every pass of this turn's
    #: loop, so it is paid for once instead of per iteration. Mutable inside a
    #: frozen dataclass on purpose: the *binding* is what must not change, and
    #: a memo that had to be threaded through `agent_node`'s signature would be
    #: passed by every caller that has no idea what it is for.
    #:
    #: Today it holds one thing: the MCP tool descriptors. Resolving those is a
    #: database read per connection plus a Redis read plus, on a cold cache, an
    #: `npx` process start — and `agent_node` was paying all of it before every
    #: single model call, while the answer cannot change mid-run. What may
    #: still change between passes (whether this run has spilled a result yet)
    #: is deliberately left out, so `read_tool_output` still appears the
    #: iteration after it becomes real.
    memo: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    #: Which agents this run may delegate to (`SubAgent` ids), or None for any
    #: the user owns. Chat passes None: a human is typing and watching, and the
    #: agents are theirs. An agent run passes its own selection, because
    #: `subAgents` was a grant with no scope — a delegating agent could reach
    #: every agent on the account, including ones holding grants it was refused.
    delegation_scope: tuple[int, ...] | None = None

    #: Sites `browser_act` may act on, or None for "ask the watching human
    #: instead" (chat). An agent run always passes a tuple — empty meaning it
    #: may browse but not act — because this scope arrived with the tool, so
    #: no agent predates it that an empty default would silently cut off.
    browser_domains: tuple[str, ...] | None = None

    #: Vault logins `browser_act` may fill (`fill_secret`), as credential
    #: slugs, or empty for none. Empty means none — unlike `browser_domains`,
    #: no run predates this scope, so there is nothing an empty default would
    #: cut off. A slug here still resolves only its owner's credential, and
    #: the value is scrubbed from everything handed back.
    browser_logins: tuple[str, ...] = ()

    #: Who an unattended run may message (`message_send`), or None for no
    #: allowlist. Chat passes None: a human is typing and watching, and
    #: approval is the gate. An agent run passes its own selection; an
    #: unattended send to anyone not on it is refused whatever the autonomy
    #: level, because autonomy decides whether a call pauses, never who it
    #: may reach. None and empty both refuse there — an allowlist nobody
    #: wrote is not an allowlist.
    recipients: tuple[str, ...] | None = None

    #: Which databases `query_sql` / `execute_sql` may touch (row ids), or
    #: None for any the user owns. An agent run passes its own selection;
    #: empty means none — the field arrives with the feature, so no agent
    #: predates it that an empty default would cut off.
    data_connections: tuple[int, ...] | None = None

    #: Which HTTP APIs `call_api` may reach (`{id: read|all}`), or None for
    #: any the user owns. Empty means none, for the same reason as above.
    api_connections: dict[int, str] | None = None

    #: Extra hosts the run may reach, added to what its connections already
    #: allow (`dbHosts` for databases, `apiHosts` for APIs). Empty: the
    #: connections' own hosts and nothing else.
    db_hosts: tuple[str, ...] = ()
    api_hosts: tuple[str, ...] = ()

    #: Extra hosts the compute workspace may reach. Empty: the default egress.
    workspace_egress: tuple[str, ...] = ()

    #: Which code projects `shell` tools may touch, or None for any owned.
    #: Empty means none — the field arrives with the feature.
    code_projects: tuple[int, ...] | None = None

    #: Which knowledge bases the KB tools may reach this turn, or None for "any
    #: the user owns". The agent runtime sets it from the builder's KB selection
    #: — which until now was read only to print names into the system prompt,
    #: while `knowledge_base_search` happily resolved any KB the user owned. A
    #: selector that narrows nothing is the permissions screen lying.
    #:
    #: None, not an empty tuple, is unrestricted: chat has no selection to make,
    #: and an agent whose selection is empty never had one enforced, so
    #: enforcing it now as "nothing" would silently empty its corpus.
    kb_scope: tuple[int, ...] | None = None

    #: Archives from *other* runs this turn may read, on top of its own. Set to
    #: the parent's session id for a delegated worker, and empty everywhere
    #: else. Read-only and one hop: a worker never writes into its parent's
    #: archive, and never sees a grandparent's.
    #:
    #: It exists because curation and delegation otherwise work against each
    #: other — a parent that curated a detail away cannot restate it in the
    #: task, and the worker, being a fresh thread, could not reach it either.
    archive_scopes: tuple[str, ...] = ()

    #: The `inference.vfs.FileScope` the file tools address this turn, or None.
    #: The agent runtime builds one from `sandbox['fileAccess']`; chat builds a
    #: fixed one (`vfs.chat_scope`: read the tree, write into `/Chat/`). None
    #: means the file tools are not offered at all, which is what a caller with
    #: nothing to address should get — an advertised tool that cannot run is
    #: worse than one never offered.
    file_scope: Any = None

    #: How many agents deep this turn already is. 0 is a run the user started;
    #: a worker spawned by `invoke_subagent` gets its parent's depth plus one.
    #: Delegation is refused past `MAX_DELEGATION_DEPTH` — without a counter,
    #: an agent holding the `subAgents` grant can invoke an agent holding the
    #: `subAgents` grant, and the cost of that is multiplicative.
    depth: int = 0

    #: `agents.budget.Deadline`, or None in chat. The instant this run must be
    #: finished by. Read in two places and nowhere else: `agent_node` stops
    #: asking for tools once it is `wrapping_up`, and `tools_node` passes it to
    #: delegating tools so a worker cannot outlive the run that asked for it.
    #: Typed `Any` for the same reason `file_scope` is — chat has no concept of
    #: an agent budget, and importing one here to name it would invert the
    #: dependency between this module and `agents/`.
    deadline: Any = None

    #: Consulted for calls `sensitive_tools` does not already name. Left None,
    #: chat's own policy applies. The agent runtime supplies a stricter one for
    #: unattended runs and an empty one for `autonomy='full'`, where the user
    #: has explicitly asked not to be interrupted.
    approval_policy: ApprovalPolicy | None = None

    #: What each autonomy level means, for the levels a user may switch to
    #: while the run is going: level -> (names that gate on sight, policy for
    #: the calls no name list can contain).
    #:
    #: Precomputed by the agent runtime and passed in, rather than resolved in
    #: `tools_node`, because working out a level's gate set needs the run's
    #: toolbox — `review` means "every tool *this agent* has" — and the toolbox
    #: is an agent concept this module knows nothing about. Left None (chat,
    #: and any caller that has not opted in), a mid-run switch is ignored and
    #: the fixed `sensitive_tools` / `approval_policy` above stand.
    approval_modes: dict[str, tuple[frozenset[str], ApprovalPolicy]] | None = None

    #: Called after every tool call finishes, with keywords:
    #: `call_id`, `name`, `args`, `output`, `status` ('completed' | 'failed'),
    #: `duration_ms`, `iteration`, `thought`. The agent runtime uses it to write
    #: one `AgentStep` row and broadcast a `node_complete` frame per call, which
    #: is what lets the canvas render an agent run. `AGENT_TRACE` alone cannot do
    #: that job: it fires *before* dispatch, so it knows neither the result nor
    #: whether the call succeeded.
    on_tool_result: ToolObserver | None = None

    #: Called once per model call, with keywords: `index` (1-based), `reasoning`
    #: (the model's thinking for *this* turn alone), `content`, `decision`
    #: ('tools' | 'answer'), `provider`, `model_id`, `tokens`, `duration_ms`.
    #: The agent runtime uses it to write one `AgentTurn` row, which is what
    #: gives every subsequent tool call something to belong to.
    #:
    #: The turn is the honest unit of an agent's work: calls issued in the same
    #: turn were decided together and their results all return to the *next*
    #: turn, never to each other. Recording it was previously left to a
    #: `{iteration, thought}` blob on each step, so the grouping could not be
    #: queried and the reasoning was a 150-character slice of it.
    on_model_turn: TurnObserver | None = None

    #: What `curate_node` is allowed to remove from the transcript when the run
    #: grows past its window. The default is disabled, so chat is untouched:
    #: chat's history is already bounded by `HISTORY_WINDOW` and its long
    #: answers by `context_summary`, and its transcript is one turn deep. The
    #: agent runtime builds a real policy from `SubAgent.runtime_settings`,
    #: where the three context-lifecycle toggles live — a long run is the case
    #: where the transcript, not the conversation, is what overflows.
    curation: Any = None

    #: Called once per curation pass, with keywords: `results_compacted`,
    #: `steps_folded`, `tokens_before`, `tokens_after`, `summary_tokens`,
    #: `archived_ids`. The agent runtime records it as a turn and streams it, so
    #: a user reading the run can see that the transcript was cut and by how
    #: much. Curation that leaves no trace is indistinguishable from a model
    #: that quietly forgot.
    on_curation: TurnObserver | None = None

    #: Whether a paused call is recorded in the HITL approval queue by this
    #: run's observer, which then owns telling the user about it.
    #:
    #: Set by the agent runtime; chat leaves it False because a chat turn has no
    #: `ExecutionLog` to hang a `HITLRequest` on — and needs none, since the
    #: person who typed the message is watching the stream that carries the
    #: prompt. Without the flag both paths would notify: `_require_approval`
    #: would write its own ad-hoc row *and* the queue's escalation ladder would
    #: write another, so one pause would reach the Inbox twice and one of the
    #: two would ignore the agent's `notifyOnHitl` setting entirely.
    approval_queue: bool = False

    #: Record a gated call instead of pausing on it (eval runs). The gate is
    #: still *decided* under the agent's own autonomy — that decision is what
    #: an eval wants to see — but nobody is there to answer it, so the call is
    #: written to `metadata['intents']` and then, per the suite's choice,
    #: `"run"` (dispatched for real) or `"block"` (answered as declined, which
    #: is what a guardrail suite proving the gate fires needs). `""` pauses as
    #: normal. A paused eval run used to open a `HITLRequest` in the owner's
    #: Inbox, start the reminder ladder for a test, and end the case as an error.
    record_intents: str = ""

    #: Whether anyone can answer an `ask_user` question during this run. True
    #: for chat, and for agent runs someone started or supervises (the manual
    #: Run button, a chat, an orchestrator that can answer through
    #: `answer_subagent`); False for schedules and triggers, where a paused
    #: question would wait for nobody. When False the question is recorded
    #: and the model proceeds on its stated assumption.
    can_ask: bool = True

    #: What started the run (`chat` | `orchestrator` | `trigger` | `api` |
    #: `eval`). Tools that are safe in a watched chat turn but not from a
    #: schedule read it — `publish_page` above `link` visibility refuses an
    #: unattended caller rather than publishing to the open internet while
    #: nobody is watching.
    caller: str = 'chat'

    #: Glob list (relative to the project root) this run may write, or None for
    #: unrestricted. Intersected parent → worker most-restrictive-wins, then
    #: with the task's claims at dispatch. Read by `ws_write/edit/apply_patch`
    #: before the lease is taken. None (not `()`) is unrestricted: `()` is what
    #: disjoint restrictions intersect to, and it means the worker may write
    #: nothing — reading it as unrestricted would widen exactly the runs the
    #: intersection refused to.
    write_paths: tuple[str, ...] | None = None

    #: Command classes `ws_run` may reach, or None for any. Same
    #: None-means-unrestricted rule as every other scope here.
    command_scope: tuple[str, ...] | None = None

    #: The task's file claims for this run (globs), or `()` when the run is
    #: not a dispatched coding task. A write must fall inside these when they
    #: are non-empty — the template's `writePaths` says where the role may
    #: ever write, the claims say where this task does.
    task_claims: tuple[str, ...] = ()

    #: Task id (`code_plan` task id) for lease labels and the panel, or ''.
    task_id: str = ''

    #: Human label for lease conflicts and approvals ("Implementer #2"), or ''.
    worker_label: str = ''

    #: This run's `ExecutionLog.execution_id`, or '' in chat. Carried so tools
    #: can attribute side effects (CodeChange, leases) to the run that made
    #: them — without it a write succeeds but leaves no record.
    execution_id: str = ''

    @property
    def max_tokens(self) -> int:
        return _MAX_TOKENS_BY_INTENT.get(self.intent, _DEFAULT_MAX_TOKENS)


def iteration_limit(intent: str) -> int:
    """Bounded but generous tool-iteration cap for the given intent."""
    if intent in ("research", "search"):
        return min(40, max(MAX_TOOL_ITERATIONS * 3, 24))
    return min(30, max(MAX_TOOL_ITERATIONS * 2, 12))


class AgentState(TypedDict):
    """Only what changes during the run. Everything static is on the config."""

    messages: Annotated[list[BaseMessage], add_messages]
    metadata: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    thinking: str
    total_tokens: int
    #: The same tokens as `total_tokens`, split into the buckets they were
    #: billed in. Carried separately rather than replacing the total because
    #: every existing reader wants the scalar — and because a total that is
    #: derived from the breakdown can never disagree with it.
    usage: TokenUsage
    #: The provider's failure on the latest model call, or "" if it answered.
    #: Overwritten every call, so only a failure that *ended* the run survives
    #: to `run_turn`, which reports it as `TurnResult.error`.
    provider_error: str


def _context(config: RunnableConfig | None) -> TurnContext:
    turn = (config or {}).get("configurable", {}).get("turn")
    if not isinstance(turn, TurnContext):
        raise RuntimeError("Agent invoked without a TurnContext on its config.")
    return turn


# ── Message threading ────────────────────────────────────────────────────────

def to_wire(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """
    Render LangChain messages as OpenAI-shaped dicts.

    Assistant turns keep their `tool_calls` and tool results keep their
    `tool_call_id`; that linkage is the whole point.
    """
    wire: list[dict[str, Any]] = []
    for message in messages:
        match message:
            case HumanMessage():
                wire.append({"role": "user", "content": message.content})
            case AIMessage():
                entry: dict[str, Any] = {
                    "role": "assistant",
                    "content": message.content or None,
                }
                if message.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call.get("args") or {}),
                            },
                        }
                        for call in message.tool_calls
                    ]
                wire.append(entry)
            case ToolMessage():
                wire.append({
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": str(message.content),
                })
            case SystemMessage():
                # Only the curator puts one of these in state, and it has to
                # reach the model: a summary of the work that was removed is
                # worthless if it is dropped on the way out, and the model would
                # then see a run that simply forgot its first thirty steps.
                wire.append({"role": "system", "content": str(message.content)})
    return wire


def _split_transcript(
    messages: Sequence[BaseMessage], *, at_limit: bool, out_of_time: bool = False
) -> tuple[list[dict[str, Any]], str]:
    """
    Split the turn transcript into (wire history, trailing prompt).

    Every provider handler builds its request as `[system] + history + [user
    prompt]` — the prompt is always appended last. So when the transcript ends
    on tool output we hand the whole thing over as history and make the required
    trailing user turn a continuation instruction, which is useful anyway. When
    it ends on the user's own message we peel that off as the prompt.
    """
    if messages and isinstance(messages[-1], HumanMessage):
        return to_wire(messages[:-1]), str(messages[-1].content)

    if out_of_time:
        nudge = prompts.CONTINUE_OUT_OF_TIME
    elif at_limit:
        nudge = prompts.CONTINUE_AT_LIMIT
    else:
        nudge = prompts.CONTINUE
    return to_wire(messages), nudge


def _turn_number(messages: Sequence[BaseMessage]) -> int:
    return sum(1 for m in messages if isinstance(m, AIMessage))


# ── Vision / attachments ─────────────────────────────────────────────────────

#: Substring hints for models absent from the AIModel registry (new OpenRouter
#: entries, mostly). The registry is authoritative; this only avoids silently
#: dropping images for a model nobody has catalogued yet.
_VISION_HINTS = (
    "vision", "-vl", "gpt-4o", "gpt-5", "gemini", "claude-", "grok-4",
    "llama-4", "pixtral", "qwen-vl", "llava", "kimi",
)


async def supports_vision(model: str, provider: str) -> bool:
    """Whether `model` accepts image input."""
    from llm.models import AIModel

    entry = await AIModel.objects.filter(value=model, provider__slug=provider).afirst()
    if entry is not None:
        return entry.supports_image_input

    lowered = model.lower()
    guessed = any(hint in lowered for hint in _VISION_HINTS)
    logger.debug("[Vision] %s not in registry; hint match=%s", model, guessed)
    return guessed


async def _describe_attachment_for_text_model(attachment, *, witness: bool) -> str:
    """Render one attachment as text for a model that cannot see it.

    With a witness available this is a pointer rather than an apology. The
    difference matters: the old text ended the matter, so the model said "I
    cannot see it" and stopped, when a model that *can* see it was one tool call
    away the whole time.
    """
    if attachment.file_type in ("image", "video"):
        if witness and attachment.file_type == "image":
            return (
                f"### Attachment: {attachment.filename} (image, id {attachment.id})\n"
                f"[You cannot see this image yourself. Call ask_vision with "
                f"attachment_id \"{attachment.id}\" to question an assistant that "
                f"can. Ask specific questions; ask follow-ups when an answer is "
                f"vague. Its replies are testimony, not your own observation.]"
            )
        return (
            f"### Attachment: {attachment.filename} ({attachment.file_type})\n"
            f"[This model has no visual input, so you cannot see this file. "
            f"Say so rather than guessing at its contents.]"
        )

    text = getattr(attachment, "extracted_text", "") or ""
    if not text:
        from inference.utils import extract_text_from_file

        path = getattr(attachment.file, "path", None) or attachment.file.name
        try:
            text = await asyncio.to_thread(
                extract_text_from_file, path, attachment.file_type
            )
        except (OSError, ValueError) as exc:
            logger.warning("[Attachments] Cannot read %s: %s", attachment.filename, exc)
            text = ""

    body = text[:20_000] if text else "[Content could not be extracted.]"
    return f"### Attachment: {attachment.filename}\n{body}"


async def prepare_attachments(
    attachments: Sequence[Any], *, model: str, provider: str,
    user_id: int | None = None,
) -> tuple[tuple[Any, ...], str]:
    """
    Split attachments into (files passed to the model, text appended to prompt).

    Vision models get the files. Text-only models get extracted text instead, so
    an upload is never silently ignored.
    """
    if not attachments:
        return (), ""

    if await supports_vision(model, provider):
        return tuple(attachments), ""

    from chat.vision import witness_available

    witness = await witness_available(user_id)
    described = [
        await _describe_attachment_for_text_model(a, witness=witness)
        for a in attachments
    ]
    return (), "\n\n## Uploaded files\n" + "\n\n".join(described)


# ── Agent node ───────────────────────────────────────────────────────────────

async def _run_model(
    turn: TurnContext,
    *,
    prompt: str,
    history: list[dict[str, Any]],
    tools: list[dict] | None,
    timings: dict[str, int] | None = None,
) -> Completion:
    """
    Call the model, streaming content to the sink as it arrives.

    Content is emitted live and retracted with CONTENT_RESET if the response
    turns out to be a preamble to a tool call. Streaming optimistically and
    correcting the rare case beats withholding every answer until we know.

    `timings` is an out-parameter, filled rather than returned, because the one
    number worth having here — how long until the *first* chunk arrived — is
    not a property of the completion and would otherwise have to be smuggled
    onto `Completion`, where every other caller would have to ignore it. Time
    to first token is the number that decides whether a slow turn is the
    provider's fault or ours: generation speed is visible in the stream, and
    everything before the first chunk is not.
    """
    accumulator = StreamAccumulator()
    call_started = time.monotonic()
    try:
        async with asyncio.timeout(LLM_CALL_TIMEOUT):
            async for chunk in llm.stream(
                provider=turn.provider,
                model=turn.model,
                prompt=prompt,
                system_message=turn.system_message,
                user_id=turn.user_id,
                temperature=turn.temperature,
                max_tokens=turn.max_tokens,
                effort=turn.effort,
                tools=tools,
                history=history,
                attachments=list(turn.attachments),
            ):
                # Any chunk counts, not only a content one: a turn that opens
                # with reasoning or with a tool call has already proved the
                # provider answered, and waiting for prose would report a
                # reasoning model as slower than it is.
                if timings is not None and "ttft_ms" not in timings:
                    timings["ttft_ms"] = int((time.monotonic() - call_started) * 1000)

                match accumulator.add(chunk):
                    case "content" if not accumulator.has_tool_calls:
                        await turn.sink(
                            Event.CONTENT_CHUNK, {"content": chunk.get("content", "")}
                        )
                    case "thinking":
                        await turn.sink(
                            Event.THINKING_CHUNK, {"content": chunk.get("content", "")}
                        )
                    case "error":
                        logger.error("[Agent] Provider error: %s", accumulator.error)
                        break
    except asyncio.TimeoutError:
        logger.warning("[Agent] Model stream exceeded %ss", LLM_CALL_TIMEOUT)
        if not accumulator.content:
            accumulator.error = "The model took too long to respond."

    completion = accumulator.finish()

    if completion.tool_calls and completion.content:
        # What we streamed was preamble, not the answer. Retract it and keep it
        # as reasoning so the work is visible but not mistaken for the reply.
        await turn.sink(Event.CONTENT_RESET, {})
        return Completion(
            content="",
            thinking=f"{completion.thinking}\n{completion.content}".strip(),
            tool_calls=completion.tool_calls,
            usage=completion.usage,
            tokens=completion.tokens,
        )

    # An out-of-credit key, a rejected key, or a model the provider has retired
    # is the user's to fix, so it is raised and reported as an error rather than
    # shown as something the assistant said. The raw provider body is never a
    # good answer either way.
    actionable = accumulator.actionable_error(turn.provider, turn.model)
    if actionable is not None and not completion.content:
        raise actionable

    if accumulator.error and not completion.content:
        # Everything else — an outage, a malformed request — still reaches the
        # user, but as a sentence rather than as the provider's JSON. The full
        # body is in the log line above for whoever has to debug it.
        sentence = humanize_provider_body(accumulator.error)
        return Completion(
            content=f"⚠️ {sentence}",
            usage=completion.usage,
            tokens=completion.tokens,
            # Carried separately so `run_turn` can report it: an agent run that
            # ended here used to close as `completed` with this as its answer
            # (benchmark, 2026-09-17: "Upstream error from Alibaba: ...").
            error=sentence or "provider error",
        )

    return completion


def _log_latency(
    turn: TurnContext,
    *,
    iteration: int,
    tools: list[dict] | None,
    tools_ms: int,
    timings: dict[str, int],
    elapsed_ms: int,
    completion: Completion,
) -> None:
    """One line per model call, saying where the wall clock actually went.

    A turn that feels slow has three candidate causes that look identical from
    outside — building the tool list, waiting for the provider's first token,
    and generating the rest — and they call for three unrelated fixes. Reported
    together they are separable at a glance; reported as one duration they are
    a guess. `cached` is here for the fourth question the other three raise: a
    provider that is re-reading the whole prefix every turn is slow for a
    reason no amount of local optimisation will touch.

    Deliberately `info` and deliberately one line. This runs on every model
    call of every turn, so it has to be cheap to emit and cheap to grep.
    """
    usage = completion.usage
    cached = getattr(usage, "cached_read", 0) or 0
    prompt_total = (getattr(usage, "input", 0) or 0) + cached
    ttft = timings.get("ttft_ms")
    logger.info(
        "[Latency] it=%d tools=%dms(n=%d) ttft=%sms total=%dms "
        "prompt=%d cached=%d(%d%%) out=%d model=%s/%s effort=%s",
        iteration,
        tools_ms,
        len(tools or ()),
        ttft if ttft is not None else "-",
        elapsed_ms,
        prompt_total,
        cached,
        round(100 * cached / prompt_total) if prompt_total else 0,
        getattr(usage, "output", 0) or 0,
        turn.provider,
        turn.model,
        turn.effort or "-",
    )


def iteration_effort(base: str | None, iteration: int, *, at_limit: bool) -> str | None:
    """The effort rung for one pass of the tool loop.

    The first pass plans the whole turn and the last permitted pass synthesises
    the final answer from everything gathered, so both run at the chosen level.
    The passes in between are mostly dispatch and incorporation — issuing the
    calls the plan already justified and reading back their results — so they
    run one rung down the ladder.

    One rung rather than a floor like `low`, because the step has to stay
    proportional to what was asked for: `high` still reasons at `medium` in
    the middle, while `low` steps to `minimal`, which `llm.access` snaps back
    to `low` on any model that does not serve it. Nothing is ever refused —
    an unknown name passes through untouched, and the snap is what keeps a
    stepped level from 400-ing on a model with fewer rungs.
    """
    from llm.effort import LADDER

    if not base or iteration == 0 or at_limit:
        return base
    try:
        rank = LADDER.index(base)
    except ValueError:
        return base
    return LADDER[max(0, rank - 1)]


async def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """One model turn: answer, or ask for tools."""
    turn = _context(config)
    iteration = _turn_number(state["messages"])
    # Two ways to reach the last pass, one mechanism. Running out of *steps* and
    # running out of *time* both mean "answer now with what you have", and both
    # are served by withholding tools below — a run stopped any other way has
    # paid for every tool call it made and returns none of what they found.
    out_of_time = turn.deadline is not None and turn.deadline.wrapping_up
    at_limit = iteration >= turn.max_iterations - 1 or out_of_time

    await turn.sink(Event.STATUS, {
        "phase": "thinking",
        "message": (
            "Wrapping up — time limit reached..." if out_of_time
            else "Thinking..." if not iteration
            else f"Reasoning (step {iteration + 1})..."
        ),
    })

    prior, prompt = _split_transcript(
        state["messages"], at_limit=at_limit, out_of_time=out_of_time,
    )
    history = list(turn.history) + prior

    # The plan, re-read rather than merely written. A list the model records and
    # never sees again is theatre: it writes one, feels organised, and proceeds
    # exactly as it would have. This lands as a trailing `system` message —
    # after the transcript, before the prompt — which is the same shape
    # `prompts.build_context_update` uses and for the same reason. It must not
    # go in the system prompt: it changes on most turns, and the system prompt
    # is the cached prefix for the whole session.
    if (plan := todos.render(state.get("metadata", {}).get("todos") or [])):
        history.append({"role": "system", "content": plan})

    # Withholding tools on the last permitted iteration is what forces an answer
    # instead of a loop that runs out of budget mid-tool-call.
    tools = None
    tools_ms = 0
    if not at_limit:
        from chat import tools as tool_registry

        tools_started = time.monotonic()
        tools = await (
            turn.tool_source()
            if turn.tool_source is not None
            else tool_registry.get_available_tools(
                turn.user_id,
                memory_enabled=turn.memory_enabled,
                session_key=turn.session_id,
                mcp_memo=turn.memo,
                file_scope=turn.file_scope,
            )
        )
        tools_ms = int((time.monotonic() - tools_started) * 1000)

    started = time.monotonic()
    timings: dict[str, int] = {}
    # The middle passes think one rung down (see `iteration_effort`). `replace`
    # rather than mutation: the context is frozen, and the caller's level is
    # what the next turn — and the latency line below — must still report.
    effective = replace(
        turn, effort=iteration_effort(turn.effort, iteration, at_limit=at_limit)
    )
    # The call below waits on a provider, not on the database. Hand this run's
    # pooled connection back first, or every iteration of every concurrent run
    # holds one for the whole model wait (G1). The next ORM call re-takes one.
    await release_db()
    try:
        completion = await _run_model(
            effective, prompt=prompt, history=history, tools=tools, timings=timings
        )
    except LLMUserActionable:
        # Deliberately not caught: no credential, no credit, or a model that no
        # longer exists. No retry and no rephrasing helps. It ends the turn as
        # an error frame the client shows as such, instead of an apology in the
        # assistant's voice that reads like the model chose not to answer.
        raise
    except LLMUnavailable as exc:
        logger.warning("[Agent] %s", exc)
        completion = Completion(content=_NO_PROVIDER_MESSAGE)
    except Exception:
        logger.exception("[Agent] Model call failed")
        completion = Completion(
            content="Something went wrong reaching the model. Please try again."
        )

    _log_latency(
        effective, iteration=iteration, tools=tools, tools_ms=tools_ms,
        timings=timings, elapsed_ms=int((time.monotonic() - started) * 1000),
        completion=completion,
    )

    calls, content = completion.tool_calls, completion.content or ""
    if at_limit and calls:
        # Tools were withheld on this last permitted call, so a call here is a
        # model ignoring that. Dispatching it would loop past the iteration cap
        # into the graph's recursion limit and lose the whole run; ending the
        # turn with whatever it wrote is the honest stop.
        logger.warning("[Agent] Dropped %d tool call(s) issued at the iteration limit", len(calls))
        calls = ()
    if not calls and content and not at_limit:
        calls, content = await _recover_text_tool_calls(content, turn, tools)

    message = AIMessage(
        content=content,
        tool_calls=[
            {"name": c.name, "args": c.arguments, "id": c.id} for c in calls
        ],
    )

    thinking = state.get("thinking", "")
    if completion.thinking:
        thinking = f"{thinking}\n\n{completion.thinking}".strip()

    if turn.on_model_turn is not None:
        # `completion.thinking`, never the accumulated `thinking`: the observer
        # writes one row per turn, and handing it the running total would make
        # each turn's reasoning a superset of the last -- growing quadratically
        # and attributing turn 1's thoughts to turn 7.
        #
        # Never allowed to break the turn. A run must not fail because
        # something was watching it, which is the same rule `on_tool_result`
        # keeps for the same reason.
        try:
            await turn.on_model_turn(
                index=iteration + 1,
                reasoning=completion.thinking or "",
                content=content,
                decision="tools" if calls else "answer",
                provider=turn.provider,
                model_id=turn.model,
                tokens=completion.tokens,
                usage=completion.usage,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Agent] on_model_turn observer raised")

    return {
        "messages": [message],
        "thinking": thinking,
        "total_tokens": state.get("total_tokens", 0) + completion.tokens,
        "usage": state.get("usage", EMPTY_USAGE) + completion.usage,
        "provider_error": completion.error,
    }


async def _recover_text_tool_calls(
    content: str, turn: TurnContext, tools: list | None = None,
) -> tuple[tuple[ToolCall, ...], str]:
    """
    Last-resort parse of tool calls a weak model wrote as text.

    SOTA models emit native `tool_calls` and never reach this; it exists for
    small local models that describe the call in prose. Returns the calls and
    the message with their raw syntax removed, so the user never sees it.
    """
    from .extraction import split_text_tool_calls

    # Only names this turn actually offered: a JSON *answer* with a "name" key
    # is otherwise read as a call to a tool that does not exist, and the run
    # loops until the recursion limit. No tools offered means nothing to recover.
    offered = {name for name in (_descriptor_name(d) for d in tools or ()) if name}
    if not offered:
        return (), content
    calls, cleaned = split_text_tool_calls(content, allowed=offered)
    if not calls:
        return (), content

    logger.info("[Agent] Recovered %d text-form tool call(s)", len(calls))
    # That text already went out as content chunks; retract it like any preamble.
    await turn.sink(Event.CONTENT_RESET, {})
    return calls, cleaned


def _descriptor_name(descriptor) -> str:
    """The tool name in an OpenAI-shaped descriptor (or a bare `{name}`)."""
    if not isinstance(descriptor, dict):
        return ""
    function = descriptor.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(descriptor.get("name") or "")


# ── Tool node ────────────────────────────────────────────────────────────────

async def _collect_media(
    result: dict, meta: dict, sink: EventSink, *, key: str, event: Event
) -> None:
    """Append `key` items from a tool result onto metadata and notify the client."""
    items = result.get(key) or []
    if not items:
        return
    merged = [*meta.get(key, []), *items]
    meta[key] = merged
    await sink(event, {key: merged})


async def _fetch_companion_images(query: str) -> list:
    """The image strip for a web search query. Never raises.

    Split out of `_on_web_search` so `tools_node` can start it while the
    web search itself is still running (see Pass 3): the two are independent
    network round trips, and awaiting one after the other added +1–2s after
    the tool the model actually needed, before the next model iteration.
    """
    try:
        from chat.sources.search import image_search

        return await image_search(query)
    except Exception:
        logger.warning("[Tools] Companion image search failed", exc_info=True)
        return []


async def _on_web_search(
    result: dict, args: dict, meta: dict, sink: EventSink,
    *, companion_images: list | None = None,
) -> None:
    if result.get("type") != "search_results":
        return

    by_url = {source.get("url"): source for source in meta.get("sources", [])}
    for source in result.get("sources", []):
        by_url.setdefault(source.get("url"), source)
    meta["sources"] = list(by_url.values())[:50]
    meta["search_query"] = args.get("query", "")
    await sink(Event.SOURCES_UPDATE, {"sources": meta["sources"]})

    # A web search also fills the image strip. The model rarely calls
    # image_search of its own accord, and a Perplexity-style answer with an
    # empty visual panel reads as broken rather than as restraint.
    #
    # When `tools_node` pre-fetched the strip alongside the search itself,
    # `companion_images` is that result (possibly `[]` on failure) and nothing
    # is awaited here — the +1–2s serial cost is gone. When None, this is a
    # direct caller outside the batch path, and the search runs inline as
    # before rather than leaving the panel empty.
    if companion_images is not None:
        await _collect_media(
            {"images": companion_images}, meta, sink,
            key="images", event=Event.IMAGES_UPDATE,
        )
    elif query := args.get("query"):
        await _collect_media(
            {"images": await _fetch_companion_images(query)}, meta, sink,
            key="images", event=Event.IMAGES_UPDATE,
        )


async def _on_image_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    await _collect_media(result, meta, sink, key="images", event=Event.IMAGES_UPDATE)


async def _on_video_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    await _collect_media(result, meta, sink, key="videos", event=Event.VIDEOS_UPDATE)


async def _on_artifact(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("type") != "html_artifact":
        return
    artifact = {k: result.get(k) for k in ("title", "html", "width", "height")}
    meta.setdefault("html_artifacts", []).append(artifact)
    await sink(Event.HTML_ARTIFACT, artifact)


async def _on_kb_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("status") != "success":
        return
    meta["kb_search_results"] = result.get("results", [])
    await sink(Event.STATUS, {
        "phase": "rag_results",
        "message": f"Found {result.get('count', 0)} relevant document chunks.",
    })


async def _on_scrape(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("status") != "success":
        return
    await sink(Event.STATUS, {
        "phase": "page_scraped",
        "message": f"Read {result.get('url', 'the page')}.",
    })


async def _on_history_search(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    # Surfaced so recalling something from 200 turns ago reads as retrieval
    # rather than the model simply having had it in context.
    if not result.get("matches"):
        return
    await sink(Event.STATUS, {
        "phase": "history_recall",
        "message": f"Recalled {result.get('returned', 0)} earlier message(s).",
    })


async def _on_deep_research(
    result: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    if result.get("sources"):
        meta["sources"] = result["sources"][:50]
        await sink(Event.SOURCES_UPDATE, {"sources": meta["sources"]})
    meta["search_queries"] = result.get("queries", [])

    # Same reasoning as the companion image search: research answers carry a
    # visual panel, and the topic is the right query for it.
    #
    # The two searches are awaited *together*. They were sequential, which cost
    # the sum of two independent network round trips — around 1.5s added to a
    # turn that had already finished its research — for no ordering reason: the
    # image strip and the video strip are separate panels, filled from separate
    # providers, and neither reads the other's result.
    #
    # Still awaited rather than detached. Backgrounding them is what the
    # latency plan asked for, and it is wrong here: `meta` is read-modify-write
    # and is handed to the observer that persists the message, so a task still
    # running when the turn closes writes its images into a dict nobody will
    # save and emits an event down a sink whose run has ended. Concurrency is
    # the part of the win that is free; detaching is the part that trades a
    # visible panel for a lost one.
    if topic := args.get("topic"):
        from chat.sources.search import image_search, video_search

        try:
            images, videos = await asyncio.gather(
                image_search(topic), video_search(topic),
                # One provider failing must not empty the other's panel, so
                # each result is inspected rather than the pair being abandoned.
                return_exceptions=True,
            )
            if not isinstance(images, BaseException):
                await _collect_media({"images": images}, meta, sink,
                                     key="images", event=Event.IMAGES_UPDATE)
            if not isinstance(videos, BaseException):
                await _collect_media({"videos": videos}, meta, sink,
                                     key="videos", event=Event.VIDEOS_UPDATE)
        except Exception:
            logger.warning("[Tools] Companion media search failed", exc_info=True)


async def _on_chart(
    parsed: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    """Collect a chart spec onto the message and stream it live.

    The spec is stored rather than a rendering of it, so a reloaded
    conversation redraws the chart with today's component — including a palette
    or an accessibility fix shipped after the chart was made. A stored SVG
    would freeze the design at the moment the model spoke.
    """
    if parsed.get("type") != "chart" or not parsed.get("series"):
        return
    spec = {k: v for k, v in parsed.items() if k != "rendered"}
    meta.setdefault("charts", []).append(spec)
    await sink(Event.CHART, spec)


async def _on_todos(
    parsed: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    """Store the run's plan in graph state and show it to the client.

    Unlike every other entry in this table, this one is not only a UI effect:
    `agent_node` reads it back on the next turn. It rides here anyway because
    `metadata` *is* graph state — `tools_node` returns it, the checkpointer
    keeps it, and `curate_node` only ever rewrites `messages`. So a plan parked
    here is immune to curation by construction rather than by anyone
    remembering to exclude it, which is the whole property the plan needs.
    """
    items = parsed.get("todos")
    if not isinstance(items, list):
        return
    meta["todos"] = items
    # Every revision is kept for the person watching, because the list is
    # replaced wholesale and a step dropped unfinished would otherwise look
    # exactly like progress (`todos.record_revision`).
    revision = todos.record_revision(meta, items)
    await sink(Event.TODOS_UPDATE, {"todos": items, "revision": revision})


#: How much of one edit's before/after text a file card keeps. The card shows a
#: diff to say *what* changed; the file itself is one click away, so the record
#: of the edit does not need to be the edit, and it rides on every reload.
FILE_EDIT_PREVIEW_CHARS = 4000

#: Edits kept per file per turn. A run that edits one file forty times needs a
#: card saying so, not forty diffs.
FILE_EDITS_KEPT = 5


async def _on_file(
    parsed: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    """Record a file the turn wrote or edited, so the answer can point at it.

    Before this, the only trace of a written file was the path the model chose
    to repeat in its prose — inert text the user had to go and find. The tool
    result already carries `document_id`, which is the one locator the file
    browser accepts, so the card links by id and never has to resolve a path.

    One entry per document, updated in place: a file written and then edited
    three times is one file, and a card per call would read as four files.
    `created` survives later edits in the same turn, because "this is new" is
    the more useful thing to know about it.
    """
    doc_id = parsed.get("document_id")
    if parsed.get("error") or not isinstance(doc_id, int):
        return

    files: list = meta.setdefault("files", [])
    entry = next((f for f in files if f.get("document_id") == doc_id), None)
    if entry is None:
        entry = {"document_id": doc_id, "edits": []}
        files.append(entry)

    path = str(parsed.get("path") or "")
    entry["path"] = path
    entry["name"] = path.rstrip("/").rsplit("/", 1)[-1] or path
    entry["chars"] = parsed.get("chars")
    if parsed.get("type"):
        # A rendered binary (`render_deck` & co.): the card shows its kind and
        # size, since its character count is of an extract, not of the file.
        entry["type"] = parsed["type"]
        entry["bytes"] = parsed.get("bytes")

    if "old_text" in args:
        action = "edited"
        if len(entry["edits"]) < FILE_EDITS_KEPT:
            entry["edits"].append({
                "old": str(args.get("old_text") or "")[:FILE_EDIT_PREVIEW_CHARS],
                "new": str(args.get("new_text") or "")[:FILE_EDIT_PREVIEW_CHARS],
                "replacements": parsed.get("replacements", 1),
            })
        else:
            entry["edits_omitted"] = entry.get("edits_omitted", 0) + 1
    elif parsed.get("created"):
        action = "created"
    elif parsed.get("appended"):
        action = "appended"
    else:
        action = "updated"
    if entry.get("action") != "created":
        entry["action"] = action

    await sink(Event.FILES_UPDATE, {"files": files})


async def _on_generated(
    parsed: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    """A generated image: a file card, plus the money it cost.

    The cost is kept on the message's metadata so the turn's price includes it
    (`pipeline` adds `tool_costs` to the model calls' cost). The agent runtime
    reads the same figure back from the step row instead.
    """
    await _on_file(parsed, args, meta, sink)
    if parsed.get("cost_usd") and not parsed.get("error"):
        meta.setdefault("tool_costs", []).append({
            "tool": "generate_image",
            "cost_usd": str(parsed["cost_usd"]),
            "cost_source": parsed.get("cost_source") or "estimated",
        })


async def _on_ws_read(
    parsed: dict, args: dict, meta: dict, sink: EventSink
) -> None:
    """Mirror a workspace read into `meta['reads']` for the run's record.

    Enforcement lives in `workspaces/reads.py` (written by the tool itself, so
    curation can never lift the guard); this is the copy the run view and the
    side panel read. Cheap — one short hash per path — and it survives
    curation because curation only ever rewrites `messages`.
    """
    if parsed.get('error'):
        return
    path = str(parsed.get('path') or args.get('path') or '').strip().lstrip('/')
    digest = str(parsed.get('sha256') or '')
    if not path or not digest:
        return
    reads = meta.setdefault('reads', {})
    reads[path] = digest


#: tool name → side effect applied to metadata / streamed to the client.
#: The model always gets the raw tool output regardless; these only drive the UI.
_SIDE_EFFECTS = {
    "web_search": _on_web_search,
    "image_search": _on_image_search,
    "video_search": _on_video_search,
    "render_html_artifact": _on_artifact,
    "knowledge_base_search": _on_kb_search,
    "scrape_webpage": _on_scrape,
    "search_conversation_history": _on_history_search,
    "deep_research": _on_deep_research,
    "update_todos": _on_todos,
    "render_chart": _on_chart,
    "write_file": _on_file,
    "edit_file": _on_file,
    "render_deck": _on_file,
    "render_workbook": _on_file,
    "render_document": _on_file,
    "generate_image": _on_generated,
    "ws_read": _on_ws_read,
}


async def _apply_side_effects(
    name: str, args: dict, raw_result: str, meta: dict, sink: EventSink,
    *, companion_images: list | None = None,
) -> None:
    handler = _SIDE_EFFECTS.get(name)
    if handler is None:
        return
    try:
        parsed = json.loads(raw_result)
    except (json.JSONDecodeError, TypeError):
        return  # tool returned prose; nothing structured to surface
    if not isinstance(parsed, dict):
        return
    try:
        if name == "web_search":
            await _on_web_search(
                parsed, args, meta, sink, companion_images=companion_images,
            )
        else:
            await handler(parsed, args, meta, sink)
    except Exception:
        # A UI side effect must never fail the turn — the model already has the
        # result, which is what actually answers the user.
        logger.exception("[Tools] Side effect for %s failed", name)


async def _require_approval(call: ToolCall, turn: TurnContext, meta: dict) -> None:
    """Pause the graph until the user approves a sensitive tool call."""
    if call.id in meta.get("approved_tool_calls", []):
        return

    logger.info("[Tools] Pausing for approval of %s", call.name)

    # Built once, read by the notification row, the SSE frame and the card. The
    # three used to phrase the same pause three ways, and the card's way was
    # `JSON.stringify(args, null, 2)`.
    from chat.tools.describe import describe_call_async

    detail = await describe_call_async(call.name, call.arguments)
    # An agent run queues the pause as a `HITLRequest` instead (see
    # `agents/agent/hitl.py`), and the reminder ladder hanging off that row is
    # what notifies — honouring the agent's `notifyOnHitl`, the user's device
    # preference and their quiet hours, none of which this call knows about.
    # Writing one here as well would put the same pause in the Inbox twice.
    if not turn.approval_queue:
        try:
            from asgiref.sync import sync_to_async
            from django.contrib.auth import get_user_model
            from notifications.utils import create_notification

            @sync_to_async
            def notify() -> None:
                create_notification(
                    user=get_user_model().objects.get(id=turn.user_id),
                    type="hitl_request",
                    title=detail["title"],
                    message=f"{detail['sentence']} Waiting for your approval.",
                    # `args` deliberately absent. This payload is rendered in
                    # the notification list, where it was printed verbatim as a
                    # JSON block under every row — a tool call's arguments are
                    # not a thing to put on a settings screen for ever.
                    # Deep link to the waiting conversation: there is no
                    # `/chat` route (chat lives at `/ai-chat`), and a bare
                    # page link leaves the user hunting for which thread
                    # paused. `session_id` is the ChatSession UUID
                    # (pipeline builds it as `str(session.id)`).
                    data={"tool": call.name, "thread_id": turn.session_id,
                          "session_id": turn.session_id,
                          "action_url": f"/ai-chat?session={turn.session_id}"},
                    # Email belongs to the digest alone: with SMTP
                    # configured this row would otherwise send one email
                    # per tool approval.
                    send_email=False,
                )

            await notify()
        except Exception:
            logger.exception("[Tools] HITL notification failed")

    await turn.sink(Event.ASK_PERMISSION, {
        # `args` still ships: the card keeps a raw disclosure behind the
        # readable fields, because that view is the one an engineer needs when
        # the sentence is wrong.
        "tool": call.name, "args": call.arguments, "call_id": call.id,
        "detail": detail,
    })
    interrupt(f"Permission required for {call.name}")


async def _require_answer(call: ToolCall, spec: dict, turn: TurnContext) -> None:
    """Pause the graph until the person answers an `ask_user` question.

    The same stop as an approval — `interrupt()`, answered from outside the
    graph (`answer_question`) so the answer survives the rollback — with a
    different frame: the chat draws a question card, and an agent run's
    observer (`agents/agent/stream.py`) files a `clarification` row in the
    Inbox. Chat also leaves a notification, as it does for approvals, because a
    question asked while the tab is in the background is otherwise invisible.
    """
    logger.info("[Tools] Pausing for an answer to %s", call.id)
    if not turn.approval_queue:
        try:
            from asgiref.sync import sync_to_async
            from django.contrib.auth import get_user_model
            from notifications.utils import create_notification

            @sync_to_async
            def notify() -> None:
                create_notification(
                    user=get_user_model().objects.get(id=turn.user_id),
                    type="hitl_request",
                    title="A question for you",
                    message=spec["question"],
                    data={"session_id": turn.session_id,
                          "action_url": f"/ai-chat?session={turn.session_id}"},
                    send_email=False,
                )

            await notify()
        except Exception:  # noqa: BLE001
            logger.exception("[Tools] Question notification failed")

    await turn.sink(Event.ASK_QUESTION, {"call_id": call.id, "tool": call.name, **spec})
    interrupt(f"Question for the user ({call.id})")


#: `answer_question`'s problem when nothing is waiting — a stale card, a row
#: from before questions paused. Callers close such a request rather than
#: report an error the person cannot act on.
NO_PAUSED_QUESTION = "That question is no longer waiting for an answer."


async def answer_question(thread_id: str, call_id: str, answer: Any) -> tuple[bool, str]:
    """Record the person's answer so the paused run resumes past its question.

    Checked against the question the run actually asked (read from the
    checkpoint, never from the client), so a card, the Inbox and a manager
    answering its worker all meet the same rule. Returns (recorded, problem).
    """
    from chat.tools.ask import QUESTION_TOOLS, normalise_answer, question_spec

    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await get_graph().aget_state(config)
    if not snapshot.values:
        return False, NO_PAUSED_QUESTION

    pending = None
    for message in reversed(snapshot.values.get("messages", [])):
        if isinstance(message, AIMessage) and message.tool_calls:
            pending = next((c for c in message.tool_calls if c.get("id") == call_id), None)
            break
    if pending is None or pending.get("name") not in QUESTION_TOOLS:
        return False, NO_PAUSED_QUESTION

    spec, problem = question_spec(pending.get("args") or {})
    if spec is None:
        return False, problem
    value, problem = normalise_answer(spec, answer)
    if value is None:
        return False, problem

    meta = dict(snapshot.values.get("metadata", {}))
    answers = dict(meta.get("question_answers", {}) or {})
    answers[call_id] = value
    meta["question_answers"] = answers
    await get_graph().aupdate_state(config, {"metadata": meta})
    logger.info("[HITL] Answered question %s on thread %s", call_id, thread_id)
    return True, ""


async def _record_approval_intent(call: ToolCall, meta: dict, iteration: int) -> None:
    """Note that this call would have paused for a human, and let it run.

    Keyed by call id so the node re-running (it is idempotent by design) never
    records one intent twice. The sentence comes from the same renderer the
    approval card uses, so an eval result reads the way the pause would have.
    """
    intents = list(meta.get("intents") or [])
    if any(i.get("call_id") == call.id for i in intents):
        return
    try:
        from chat.tools.describe import describe_call_async

        sentence = (await describe_call_async(call.name, call.arguments)).get("sentence", "")
    except Exception:  # noqa: BLE001 - a missing sentence must not stop the run
        sentence = ""
    intents.append({
        "kind": "approval", "tool": call.name, "args": call.arguments,
        "call_id": call.id, "iteration": iteration, "sentence": sentence,
    })
    meta["intents"] = intents


def _refusal_text(name: str, reason: str) -> str:
    """What the model is told when the user declines a call it asked for."""
    reason = (reason or "").strip()
    from chat.tools.ask import QUESTION_TOOLS

    if name in QUESTION_TOOLS:
        return (
            "The user skipped this question. Proceed on your stated assumption "
            "and do not ask it again; say in your answer what you assumed."
        )
    base = f"The user declined to run {name}."
    if reason:
        base = f"{base} Reason: {reason}"
    return (
        f"{base} Do not retry it. Continue with what you can do without it, "
        f"or explain what you now cannot do."
    )


@dataclass
class _Batch:
    """Everything one `tools_node` call shares across its four passes.

    One object rather than a dozen arguments, because the passes really do
    share it: pass 1 adds to `rejected` (an eval blocking a call), pass 2 pops
    from it (a manager's decision taking the refusal over), and pass 4 reads
    what is left. Built once per batch by `tools_node` and never stored.
    """

    turn: TurnContext
    calls: list[ToolCall]
    meta: dict[str, Any]
    trace: list[dict[str, Any]]
    iteration: int
    reasoning: str
    #: What every dispatched tool is handed, before its per-call fields.
    tool_context: dict[str, Any]
    #: Names that pause on sight, and the policy judging everything else.
    sensitive: frozenset
    policy: Any
    dispatch: Any
    #: call id -> why it will not run (the user declined, or an eval blocked it).
    rejected: dict[str, str]
    #: call ids the user already approved; never re-judged.
    approved: set[str]
    #: call id -> the person's answer to an `ask_user` card.
    answers: dict[str, Any]
    #: call id -> (decision, reason) the person gave on an `answer_subagent` card.
    decided: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: (call, arguments) for every call that will be dispatched, in call order.
    planned: list[tuple[ToolCall, dict]] = field(default_factory=list)
    #: call id -> (output, status, duration_ms).
    outcomes: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    #: call id -> the companion image-strip task started beside a web search.
    companions: dict[str, asyncio.Task] = field(default_factory=dict)


def _tool_context(turn: TurnContext, state: AgentState) -> dict[str, Any]:
    """The context dict every tool call in this batch is dispatched with."""
    tool_context = {
        "user_id": turn.user_id,
        "session_id": turn.session_id,
        "turn_id": turn.turn_id,
        "depth": turn.depth,
        # The run's own clock, so a tool that starts *other* runs can bound
        # them by what is left of it. Only `invoke_subagent` reads it; every
        # other tool is bounded by its own timeout and by the loop stopping.
        "deadline": turn.deadline,
        # None in chat. The file tools read it to find the subtree they may
        # address, and answer "no file access" rather than guessing a default —
        # a default here would be a scope nobody granted.
        "file_scope": turn.file_scope,
        # Same shape, same reasoning, for the knowledge bases: the KB tools
        # filter on it rather than resolving anything the user owns.
        "kb_scope": turn.kb_scope,
        # And the same again for delegation. `subAgents` was a grant with no
        # scope at all: `search_agents` and `run_agent` filtered on `user_id`
        # and nothing else, so an agent that could delegate could discover and
        # run *every* agent its owner had — including ones with wider grants
        # than its own, which makes delegation a way to reach a tool you were
        # not given. None is unrestricted, as everywhere else.
        "delegation_scope": turn.delegation_scope,
        "browser_domains": turn.browser_domains,
        "browser_logins": turn.browser_logins,
        "recipients": turn.recipients,
        "data_connections": turn.data_connections,
        "api_connections": turn.api_connections,
        "db_hosts": turn.db_hosts,
        "api_hosts": turn.api_hosts,
        # Extra archives the retrieval tools may read — a worker's parent.
        "archive_scopes": turn.archive_scopes,
        # This run's per-tool allow/ask/deny. Read by `invoke_subagent`, so
        # a worker inherits its parent's restrictions most-restrictive-wins
        # rather than re-widening them by holding a looser row.
        "tool_permissions": dict(turn.tool_permissions or {}),
        # Who started the run. `publish_page` refuses above-`link`
        # visibilities from unattended callers.
        "caller": turn.caller,
        # Coding-team scopes (C1/C2). None means unrestricted, as everywhere
        # else — a run that predates the field keeps today's behaviour.
        "write_paths": turn.write_paths,
        "command_scope": turn.command_scope,
        "task_claims": tuple(turn.task_claims or ()),
        "task_id": turn.task_id,
        "worker_label": turn.worker_label,
        "execution_id": turn.execution_id,
        # The lead's live sink, so detached workers can publish plan-panel
        # frames (task/lease/change updates) to whoever is watching the lead.
        # None in unit tests that build a bare TurnContext.
        "sink": turn.sink,
    }
    # Provenance, recomputed per batch because each batch adds tool results.
    # `known_urls` is every URL someone other than the model wrote — the user,
    # the agent's author, an earlier tool result — so the read tools can refuse
    # a URL the model composed to carry data out (`core/safety/provenance.py`).
    # `tainted_by` names the first tool whose result this turn reads like
    # orders to an AI; the `auto` reviewer then stops allowing irreversible
    # calls without a human. Only third-party text is scanned for that — the
    # user's own message is the principal, not an injection.
    from core.safety import provenance

    tool_texts = [
        (message.name or "tool", message.content if isinstance(message.content, str)
         else str(message.content))
        for message in state["messages"] if isinstance(message, ToolMessage)
    ]
    tool_context["known_urls"] = frozenset(provenance.urls_in(
        [turn.user_text, turn.system_message]
        + [str(entry.get("content") or "") for entry in turn.history
           if isinstance(entry, dict)]
        + [text for _, text in tool_texts]
    ))
    tool_context["tainted_by"] = next(
        (name for name, text in tool_texts if provenance.instruction_shaped(text)),
        None,
    )
    return tool_context


def _approval_rules(turn: TurnContext) -> tuple[frozenset, Any]:
    """(names that pause on sight, policy for the rest) for this batch."""
    from chat.tools import permissions
    from chat import tools as tool_registry

    sensitive = (
        turn.sensitive_tools
        if turn.sensitive_tools is not None
        else frozenset(tool_registry.SENSITIVE_TOOLS)
    )
    policy = turn.approval_policy or permissions.default_policy

    # A user watching the run may have loosened (or tightened) how much it asks
    # since the last batch. Read here, per batch, rather than captured with the
    # rest of the turn: `TurnContext` is frozen, and a mode that could only be
    # chosen before the run started is the thing this is fixing. The override
    # is not drained — it stands for the rest of the run.
    if turn.approval_modes:
        from . import steering

        chosen = steering.autonomy(turn.session_id)
        if chosen and chosen in turn.approval_modes:
            sensitive, policy = turn.approval_modes[chosen]

    # Per-tool ask/allow, folded in after the autonomy level (and any mid-run
    # switch of it) is resolved, so the overrides survive a switch. `ask`
    # pauses even under `auto`/`full`; `allow` frees even under `ask` or
    # `review`. `deny` is not a gate — denied tools are withheld and refused
    # by the toolbox before this node ever sees them.
    if turn.tool_permissions:
        sensitive = permissions.apply_tool_permission_overrides(
            sensitive, turn.tool_permissions)
    return sensitive, policy


def _audit(batch: _Batch, name: str, arguments: dict) -> dict | None:
    """What the `auto` reviewer decided about this call, if it was asked.

    Read off the policy pass 1 actually consulted — a mid-run switch may have
    replaced the turn's own — never the turn's, which may be neither. Never
    raises: an audit must not break the plan or the record.
    """
    try:
        from .reviewer import audit_for

        return audit_for(batch.policy, name, arguments)
    except Exception:  # noqa: BLE001
        return None


async def tools_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Execute every tool the last assistant turn asked for, in four passes.

    1. `_settle_gates`: every approval and question, before anything runs.
    2. `_plan_calls`: arguments and trace entries, in call order.
    3. `_dispatch_calls`: the safe calls together, the rest one by one.
    4. `_record_results`: observe, apply side effects and answer, in call order.

    The order is the design. Settling first makes a resumed node idempotent;
    planning and recording in call order keep the transcript, the step rows and
    the UI identical whichever tool finishes first. Each pass says why.
    """
    from chat import tools as tool_registry

    turn = _context(config)
    last = state["messages"][-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"messages": [], "metadata": dict(state.get("metadata", {}))}

    meta = dict(state.get("metadata", {}))
    sensitive, policy = _approval_rules(turn)
    batch = _Batch(
        turn=turn,
        calls=[
            ToolCall(id=raw["id"], name=raw["name"], arguments=dict(raw.get("args") or {}))
            for raw in last.tool_calls
        ],
        meta=meta,
        trace=list(state.get("tool_trace", [])),
        iteration=_turn_number(state["messages"]),
        reasoning=(state.get("thinking") or "").strip()[-150:],
        tool_context=_tool_context(turn, state),
        sensitive=sensitive,
        policy=policy,
        dispatch=turn.tool_dispatch or tool_registry.execute_chat_tool,
        rejected=dict(meta.get("rejected_tool_calls", {}) or {}),
        approved=set(meta.get("approved_tool_calls", []) or []),
        answers=dict(meta.get("question_answers", {}) or {}),
    )

    await _settle_gates(batch)
    await _plan_calls(batch)
    await _dispatch_calls(batch)
    results = await _record_results(batch)
    return {"messages": results, "metadata": batch.meta, "tool_trace": batch.trace}


async def _settle_gates(batch: _Batch) -> None:
    """Pass 1: settle permission for every call before dispatching any.

    This runs as its own pass rather than inline with dispatch because
    `interrupt()` discards the node's writes and re-runs it from the top on
    resume. Interleaved, a batch of [safe, sensitive] would dispatch the safe
    call, pause on the sensitive one, and then dispatch the safe one *a
    second time* when the user approved — sending the email twice, writing a
    second `AgentStep` row, and re-firing the UI side effects. Graph
    state is rolled back by the interrupt; the outside world is not.

    Settling every permission first makes the node's re-run idempotent: the
    only work before the pause is asking, and the answers persist in
    `metadata` (written by `approve_tool_call` / `reject_tool_call` from
    outside the node, so they survive the rollback).

    The gates are *decided* concurrently and *acted on* in call order. Under
    chat `auto` a policy is a model call (~1.5 s), and awaiting them one at
    a time made a batch of four writes a six-second silence; every policy is
    a pure read, so overlapping them changes nothing but the wait. A call
    the user already approved is not re-judged: the resumed node re-runs
    this pass for the whole batch, and asking the policy again could turn
    an answer the user just gave into a second approval card.
    """
    from chat.tools.ask import QUESTION_TOOLS, question_spec
    from chat.tools.agents import SUBAGENT_ANSWER_TOOL, subagent_answer_needs_user

    turn = batch.turn

    async def _gated(call: ToolCall) -> bool:
        if call.id in batch.rejected or call.id in batch.approved:
            return False
        # A question is not an approval: it pauses below on its own terms, in
        # every mode, and asking permission to ask would be two cards for one.
        if call.name in QUESTION_TOOLS:
            return False
        # A manager answering its worker: questions and refusals are its own
        # call in any mode; only letting the worker *act* can need the boss.
        if call.name == SUBAGENT_ANSWER_TOOL and not await subagent_answer_needs_user(
                call.arguments, batch.tool_context):
            return False
        # Two gates, checked cheapest first. The name list carries reasoning
        # about tools we wrote; the policy inspects calls nobody could have
        # listed in advance. Either one is enough to pause. The call id and
        # the live plan ride along so the `auto` reviewer can cache its
        # verdict per call and judge against the plan as it now stands.
        return call.name in batch.sensitive or await batch.policy(
            call.name, call.arguments,
            {**batch.tool_context, "call_id": call.id,
             "todos": list(batch.meta.get("todos") or [])},
        )

    gates = await asyncio.gather(*(_gated(call) for call in batch.calls))
    for call, gated in zip(batch.calls, gates):
        if (call.name in QUESTION_TOOLS and call.id not in batch.answers
                and call.id not in batch.rejected and turn.can_ask
                and not turn.record_intents):
            spec, _problem = question_spec(call.arguments)
            # A malformed question is not drawn: the tool answers with the
            # problem and the model re-asks. A card nobody can fill in is
            # worse than no card.
            if spec is not None:
                await _require_answer(call, spec, turn)
            continue
        if gated:
            if turn.record_intents:
                await _record_approval_intent(call, batch.meta, batch.iteration)
                if turn.record_intents == "block":
                    batch.rejected[call.id] = (
                        "This is an evaluation run: the call would have waited "
                        "for approval, so it was recorded and not run."
                    )
                continue
            await _require_approval(call, turn, batch.meta)


async def _plan_calls(batch: _Batch) -> None:
    """Pass 2: plan every call, in call order.

    Arguments, trace entries and the AGENT_TRACE frames are all built here,
    before anything is dispatched, so what the UI is told never depends on
    which tool happens to finish first.
    """
    from chat.tools.agents import SUBAGENT_ANSWER_TOOL

    # When the person answered a manager's `answer_subagent` card, their answer
    # *is* the decision, whatever the model proposed: Approve lets the worker
    # act, Deny refuses it. A denied card is therefore still dispatched — as a
    # refusal — because a worker left waiting on an unanswered question pauses
    # for ever.
    for call in batch.calls:
        if call.name != SUBAGENT_ANSWER_TOOL:
            continue
        if call.id in batch.rejected:
            batch.decided[call.id] = (
                "reject", batch.rejected.pop(call.id) or "The user declined.")
        elif call.id in batch.approved:
            batch.decided[call.id] = ("approve", "")

    for call in batch.calls:
        refusal = batch.rejected.get(call.id)
        if refusal is not None:
            # A declined call still owes the model a `tool` message: the
            # assistant turn requested it by id, and a transcript with a
            # dangling `tool_call_id` is malformed. Answering with the refusal
            # is also what lets the model adapt — before this, a rejection left
            # the graph paused for ever, because nothing ever resumed it.
            batch.trace.append({"tool": call.name, "args": call.arguments,
                                "iteration": batch.iteration, "thought": batch.reasoning,
                                "summary": "declined by user", "call_id": call.id,
                                "status": "rejected"})
            continue

        # web_search with no query is the one omission worth repairing rather
        # than bouncing back — the user's own message is always the right query.
        arguments = dict(call.arguments)
        if call.name == "web_search" and not arguments.get("query"):
            arguments["query"] = batch.turn.user_text
        if call.id in batch.decided:
            decision, reason = batch.decided[call.id]
            arguments["decision"] = decision
            if reason:
                arguments["reason"] = reason

        entry = {"tool": call.name, "args": arguments, "iteration": batch.iteration,
                 "thought": batch.reasoning, "summary": batch.reasoning,
                 "call_id": call.id}
        # Filed under the plan step in progress when the call was made, so the
        # plan can show what each step actually did. The plan as it stood at
        # the start of this batch: an `update_todos` in the same batch applies
        # after, and it is not itself work on a step.
        if call.name != "update_todos" and (
                step := todos.current_step(batch.meta.get("todos") or [])):
            entry["step"] = step
        # An `auto` reviewer that let this through (or asked about it) leaves
        # its audit on the trace entry, so the run stays auditable afterwards.
        if (approval := _audit(batch, call.name, arguments)) is not None:
            entry["approval"] = approval
        batch.trace.append(entry)
        await batch.turn.sink(Event.AGENT_TRACE, {"sub_type": "tool", **entry})
        batch.planned.append((call, arguments))


async def _dispatch_one(batch: _Batch, call: ToolCall, arguments: dict) -> tuple[str, str, int]:
    """Run one call. Returns (output, status, duration_ms); never raises."""
    # Its own copy of the context. `call_id` used to be written onto the
    # single shared dict immediately before each dispatch, which is exactly
    # the field a concurrent sibling would overwrite — and
    # `invoke_subagent` reads it to record which tool call spawned a
    # worker, so a race there misattributes whole runs.
    ctx = {**batch.tool_context, "call_id": call.id,
           # The person's answer to an `ask_user` card, and whether a
           # manager's decision about its worker came from the person.
           "answered": call.id in batch.answers,
           "user_answer": batch.answers.get(call.id),
           "decided_by_user": call.id in batch.decided}
    started = time.monotonic()
    try:
        async with asyncio.timeout(TOOL_CALL_TIMEOUT):
            output = await batch.dispatch(call.name, arguments, ctx)
        status = "completed"
    except asyncio.TimeoutError:
        status = "failed"
        output = f"Error: {call.name} timed out after {TOOL_CALL_TIMEOUT}s."
    except Exception as exc:  # noqa: BLE001
        logger.exception("[Tools] %s raised", call.name)
        status = "failed"
        output = f"Error running {call.name}: {exc}"
    return output, status, int((time.monotonic() - started) * 1000)


async def _dispatch_calls(batch: _Batch) -> None:
    """Pass 3: dispatch — the read-only calls together, the rest one by one.

    A model issues every call in a turn before seeing any result, so nothing
    in this batch can depend on anything else in it and overlapping is safe
    by construction. Sensitive calls are excluded whatever else is true,
    because a tool worth pausing a human for is a tool with a side effect,
    and two of those in one turn may well be ordered.

    Two ways in, and the difference between them is who is making the claim.
    A built-in declares `parallel=True` on `@tool()` — a statement by whoever
    wrote it. An MCP tool cannot declare anything: its name is minted at
    runtime by a third party, which is why `PARALLEL_TOOLS` alone left every
    connector call serial, and three read calls to one server cost three
    round trips end to end.

    `mcp_reads_only` is the second way in, and it is a *guess from the name*,
    which needs justifying because the docs elsewhere say unknown means
    serial. The justification is that this codebase already trusts exactly
    this guess for a strictly stronger decision — `default_policy` uses it to
    decide whether a credentialed call is gated at all, and the connector
    `read` scope uses it to decide which tools an agent is even offered. What
    a wrong guess costs here is far less than what it costs there: mis-reading
    a destructive name means an unapproved deletion when it decides gating,
    but only means that a deletion already issued in this turn overlapped a
    sibling when it decides scheduling. No name can cause a call the model
    did not make.
    """
    from chat.tools import permissions
    from chat import tools as tool_registry

    def _may_overlap(call) -> bool:
        if call.name in batch.sensitive:
            return False
        if call.name in tool_registry.PARALLEL_TOOLS:
            return True
        return permissions.mcp_reads_only(call.name)

    concurrent = [(c, a) for c, a in batch.planned if _may_overlap(c)]
    serial = [(c, a) for c, a in batch.planned if not _may_overlap(c)]

    # Companion image strips start here, alongside the searches they belong
    # to, rather than after every dispatch has finished (see
    # `_fetch_companion_images`). The query is known at plan time, the strip
    # is an independent network round trip, and the strip is only ever read
    # in pass 4 — which writes `meta` and persists it — so nothing here
    # outlives the turn the way a detached background task would. Skipped
    # when the model already asked for images itself: `_on_image_search`
    # fills the same panel, and a second query for it would be pure spend.
    if not any(c.name == "image_search" for c, _ in batch.planned):
        for call, arguments in batch.planned:
            if call.name == "web_search" and arguments.get("query"):
                batch.companions[call.id] = asyncio.create_task(
                    _fetch_companion_images(arguments["query"])
                )

    try:
        if len(concurrent) > 1:
            logger.info("[Tools] iter=%d dispatching %d calls in parallel: %s",
                        batch.iteration, len(concurrent), [c.name for c, _ in concurrent])
            gathered = await asyncio.gather(
                *(_dispatch_one(batch, c, a) for c, a in concurrent)
            )
            batch.outcomes.update({c.id: o for (c, _), o in zip(concurrent, gathered)})
        else:
            serial = concurrent + serial      # a lone call gains nothing from gather

        for call, arguments in serial:
            logger.info("[Tools] iter=%d %s(%s)", batch.iteration, call.name, sorted(arguments))
            batch.outcomes[call.id] = await _dispatch_one(batch, call, arguments)
    finally:
        # Never leave a companion running past the dispatches: pass 4 awaits
        # each one it needs, and anything unneeded (a declined call, a
        # non-search result) is cancelled here rather than writing into a
        # `meta` nobody will persist.
        for call_id, task in batch.companions.items():
            if call_id not in batch.outcomes and not task.done():
                task.cancel()


async def _record_results(batch: _Batch) -> list[ToolMessage]:
    """Pass 4: observe and record, in call order.

    Deliberately not inside the dispatch. `_apply_side_effects` does a
    read-modify-write on the shared `meta` (see `_collect_media`), and the
    observer writes one `AgentStep` row per call — doing either in completion
    order would make the transcript, the step rows and the UI's search
    results reshuffle between runs of the same turn.
    """
    from chat.tools import tool_output

    turn = batch.turn
    results: list[ToolMessage] = []
    args_by_id = {call.id: arguments for call, arguments in batch.planned}
    for call in batch.calls:
        refusal = batch.rejected.get(call.id)
        if refusal is not None:
            results.append(ToolMessage(
                content=_refusal_text(call.name, refusal),
                tool_call_id=call.id, name=call.name,
            ))
            continue

        output, status, duration_ms = batch.outcomes[call.id]
        arguments = args_by_id[call.id]

        if turn.on_tool_result is not None:
            # Never let an observer break a tool call that already succeeded —
            # it exists to watch the run, not to take part in it.
            try:
                await turn.on_tool_result(
                    call_id=call.id, name=call.name, args=arguments,
                    output=output, status=status, duration_ms=duration_ms,
                    iteration=batch.iteration, thought=batch.reasoning,
                    approval=_audit(batch, call.name, arguments),
                )
            except Exception:  # noqa: BLE001
                logger.exception("[Tools] on_tool_result observer raised")

        # The companion has been running since pass 3, alongside the search
        # itself — by now it is usually already done, so this await costs
        # nothing and the strip lands in the same `meta` write as the sources.
        companion_images: list | None = None
        if (task := batch.companions.get(call.id)) is not None:
            try:
                async with asyncio.timeout(15):
                    companion_images = await task
            except (asyncio.TimeoutError, asyncio.CancelledError):
                companion_images = []
            except Exception:  # noqa: BLE001 — never fail a search over its strip
                companion_images = []

        await _apply_side_effects(
            call.name, arguments, output, batch.meta, turn.sink,
            companion_images=companion_images,
        )

        # Bounded here and nowhere earlier: the observer above wants the whole
        # result for its durable log, and the side effects parse it as JSON to
        # drive the UI. Both are done with it by this point, so the only reader
        # left is the model — which is the one that has to pay for every
        # character. Anything trimmed is stored and named in what comes back.
        model_output = await tool_output.bound(
            call.name, output, {**batch.tool_context, "call_id": call.id})
        results.append(
            ToolMessage(content=model_output, tool_call_id=call.id, name=call.name)
        )
    return results


async def steering_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Pick up anything the user said — and anything that changed — while the run worked.

    A plain node, deliberately. `interrupt()` exists to stop and wait for an
    actor outside the graph; a steer is already in the mailbox by the time this
    runs, so there is nothing to wait for — and using an interrupt here would
    give `run_turn`'s pause detection a second reason to fire that it would
    then have to tell apart from an approval.

    Costs two dict lookups per tool round when nobody is steering.

    Steers drain as a `HumanMessage` (the user talking); change notices drain
    as a `system` context message (information, not instruction). The notice
    goes first so a trailing steer is still peeled as the turn's prompt by
    `_split_transcript` — a notice must never read as a user turn.

    Returns nothing (not even passthrough state): like `curate_node`, it
    rewrites `messages` and nothing else, so the plan parked in `metadata`
    stays immune by construction (`test_todos.py` pins this shape).
    """
    from . import steering

    turn = _context(config)
    message = steering.take(turn.session_id)
    notices = steering.take_notices(turn.session_id)
    if not message and not notices:
        return {}

    out_messages: list = []
    if notices:
        logger.info("[Steer] Delivering %s into %s", 'a change notice', turn.session_id)
        out_messages.append(SystemMessage(content=notices))
    if message:
        logger.info("[Steer] Delivering a steer into %s", turn.session_id)
        await turn.sink(Event.STATUS, {
            "phase": "steered",
            "message": "Picking up your message...",
        })
        # A user message, not a system one: it is the user talking, and the model
        # already knows how to weigh a later instruction against an earlier one.
        out_messages.append(HumanMessage(content=message))
    return {"messages": out_messages}


# ── Curation node ────────────────────────────────────────────────────────────

async def _summariser_for(turn: TurnContext):
    """The `async (text) -> (summary, tokens)` the curator folds with.

    A pinned cheap model by default rather than the run's own: a forty-turn run
    on an expensive model would otherwise pay full rate to compress itself, and
    the fold is an extractive job that does not need the model the user chose
    the agent for. Falls back to the run's model when nothing is pinned, because
    a fold that cannot run at all is worse than one that costs a little.
    """
    from . import curation

    policy = turn.curation
    provider = policy.summary_provider or turn.provider
    model = policy.summary_model or turn.model

    async def _call(provider_: str, model_: str, text: str) -> tuple[str, int]:
        completion = await llm.complete(
            provider=provider_,
            model=model_,
            prompt=text,
            system_message=curation.SUMMARY_INSTRUCTION,
            user_id=turn.user_id,
            temperature=0,
            max_tokens=1_024,
            # The fold is extractive: it rewrites text that already exists.
            # Paying the run's chosen effort to compress its own transcript is
            # the one place the knob would cost money for nothing.
            effort="none",
        )
        # The fold is a real model call on a model of its own, so its usage is
        # kept here rather than discarded with the rest of the completion.
        # Without this the fold's spend reached `total_tokens` but never
        # `cost_usd`, and the cap that curation exists to serve could not see
        # the money curation itself was spending.
        summarise.usage += completion.usage
        summarise.model_id = model_
        return completion.content or "", completion.tokens

    async def summarise(text: str) -> tuple[str, int]:
        try:
            return await _call(provider, model, text)
        except LLMUserActionable:
            # No credential for the pinned model, no credit, or it has been
            # retired. The run's own model is known to work — this turn has been
            # using it — so falling back keeps the note rather than losing the
            # steps behind it. Only worth trying when it is a *different* call.
            if (provider, model) == (turn.provider, turn.model):
                raise
            logger.warning(
                "[Curation] Fold model %s/%s unusable; folding with the run's own",
                provider, model,
            )
            return await _call(turn.provider, turn.model, text)

    # Attributes rather than a closure variable so `curate_node` can read them
    # back without `curation.py` — which is deliberately provider-agnostic and
    # knows nothing about money — having to carry them through its signature.
    summarise.usage = EMPTY_USAGE
    summarise.model_id = model

    return summarise


@sync_to_async
def _price_fold(summariser) -> tuple[Decimal, str]:
    """What the fold's own model call cost.

    Never raises: curation is a cost control, and failing to price it must not
    fail the run any more than failing to perform it would.
    """
    from llm.pricing import cost_for_usage

    usage = getattr(summariser, "usage", EMPTY_USAGE)
    if usage.is_empty:
        return Decimal("0"), ""
    try:
        return cost_for_usage(getattr(summariser, "model_id", "") or "", usage)
    except Exception:  # noqa: BLE001
        logger.exception("[Curation] Pricing the fold failed")
        return Decimal("0"), "unpriced"


async def curate_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Cut the transcript back when it has grown past the model's window.

    A node rather than a step inside `agent_node` because the cut has to reach
    graph state: replacements carry the ids they replace, so `add_messages`
    substitutes them in the checkpoint and the next turn starts from the curated
    transcript. Doing it on the outgoing copy would re-do — and re-archive — the
    same work on every remaining turn of the run.

    It sits on the tools -> agent path because that is the only place the
    transcript grows. Returning `{}` when there is nothing to do is the common
    case and costs one token estimate.
    """
    turn = _context(config)
    if turn.curation is None or not getattr(turn.curation, "enabled", False):
        return {}

    from . import curation

    summariser = await _summariser_for(turn)
    try:
        result = await curation.curate(
            state["messages"],
            policy=turn.curation,
            model=turn.model,
            reserve_output=turn.max_tokens,
            # The system message and any conversation outside the run are
            # already spoken for; the watermark has to be measured against the
            # whole request, not against the part that lives in graph state.
            baseline_tokens=(
                llm.estimate_tokens(turn.system_message)
                + sum(llm.estimate_tokens(str(e.get("content") or "")) for e in turn.history)
            ),
            context={
                "user_id": turn.user_id,
                "session_id": turn.session_id,
                "turn_id": turn.turn_id,
            },
            summarise=summariser,
        )
    except Exception:  # noqa: BLE001
        # Curation is a cost control, not a correctness one — `clamp_input` is
        # still behind it. A run must never fail because the thing that keeps it
        # cheap broke.
        logger.exception("[Agent] Curation failed; transcript left as it was")
        return {}

    if not result.curated:
        return {}

    await turn.sink(Event.STATUS, {
        "phase": "curating",
        "message": "Condensing earlier steps to stay inside the context window...",
    })

    # Priced against the fold's own model, which is usually not the run's — a
    # pinned cheap one. Charging it at the run's rate would overstate exactly
    # the mechanism that exists to save money.
    fold_cost, fold_source = await _price_fold(summariser)

    if turn.on_curation is not None:
        try:
            await turn.on_curation(
                results_compacted=result.results_compacted,
                steps_folded=result.steps_folded,
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after,
                summary_tokens=result.summary_tokens,
                archived_ids=result.archived_ids,
                summary_cost_usd=fold_cost,
                summary_cost_source=fold_source,
            )
        except Exception:  # noqa: BLE001
            logger.exception("[Agent] on_curation observer raised")

    return {
        "messages": result.updates,
        # The fold is a real model call and is charged for like one: it counts
        # against the run's token total, and therefore against the spend cap.
        # A summariser that spent money invisibly would be a hole in the
        # guardrail it is meant to serve.
        "total_tokens": state.get("total_tokens", 0) + result.summary_tokens,
    }


# ── Graph ────────────────────────────────────────────────────────────────────

#: Graph nodes visited per tool iteration: agent, tools, curate, steering. Keep
#: in step with `_build_graph`; `run_turn` sizes the recursion limit from it and
#: `chat/tests/test_iteration_limit.py` fails if a node is added without it.
STEPS_PER_ITERATION = 4


def _next_step(state: AgentState) -> str:
    last = state["messages"][-1]
    return "tools" if isinstance(last, AIMessage) and last.tool_calls else END


def _build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.add_node("steering", steering_node)
    graph.add_node("curate", curate_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _next_step, {"tools": "tools", END: END})
    # tools -> steering -> agent, rather than tools -> agent. The steer lands
    # *after* the tool results are threaded and *before* the model reads them,
    # which is the only boundary where a new user message cannot separate an
    # assistant turn from the `tool` messages answering its call ids.
    # Curation goes between them, not after: it is the tool results that make a
    # transcript outgrow its window, so this is the only edge where cutting is
    # ever needed — and it must run *before* the steer so the steer stays the
    # last message in state. `_split_transcript` peels a trailing `HumanMessage`
    # off as the turn's prompt, which is exactly where a new instruction
    # belongs; a summary note appended after it would take that place instead.
    graph.add_edge("tools", "curate")
    graph.add_edge("curate", "steering")
    graph.add_edge("steering", "agent")
    # Durability is a setting, not a constant — see `chat/turn/checkpoints.py`.
    # In-process was fine while a run meant a chat turn; an agent run can go
    # forty iterations across two hours, and losing that to a deploy leaves an
    # `ExecutionLog` stuck on `running` with nothing behind it.
    return graph.compile(checkpointer=checkpoints.build())


#: Built on first use, not at import. The durable savers construct an event-loop
#: binding in `__init__` (`AsyncSqliteSaver` calls `get_running_loop()`), and
#: module import happens on whatever thread Django starts on, with no loop —
#: so an eagerly compiled graph could only ever have an in-process saver. Every
#: real caller reaches the graph from async code, so deferring costs nothing.
#:
#: `chat_agent_graph` still resolves as a module attribute (PEP 562 below), so
#: `from chat.turn.agent import chat_agent_graph` keeps working.
_graph = None


def get_graph():
    """The compiled agent graph for this process. Built once, on first use."""
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


def __getattr__(name):
    if name == "chat_agent_graph":
        return get_graph()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def forget_thread(thread_id: str) -> bool:
    """Drop one thread's checkpoints from the in-process saver.

    `MemorySaver` has no eviction: no `maxsize`, no TTL, nothing that ever
    expires. Every super-step of every run it has ever checkpointed stays
    resident for the life of the process, and a run's transcript grows
    quadratically in its own iteration count — so the process grows without
    bound while nothing is leaking in the ordinary sense. Agent runs made it
    sharpest, because each one gets a fresh uuid thread id and every fanout
    worker gets another: once a run is finished, its thread can never be
    reached again, and nothing was deleting it.

    Only ever call this on a run that has actually ended. A *paused* run is
    exactly the case that needs its checkpoint kept — the approval resumes from
    it, and dropping it would strand the run the user is being asked about.

    Best-effort by design: failing to free memory must not fail a run that has
    already produced its answer.
    """
    try:
        checkpointer = getattr(get_graph(), "checkpointer", None)
        deleter = getattr(checkpointer, "adelete_thread", None)
        if deleter is None:
            return False
        await deleter(thread_id)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("[Agent] Could not drop checkpoints for %s", thread_id,
                       exc_info=True)
        return False


# ── Public API ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class TurnResult:
    """What one agent turn produced."""

    answer: str = ""
    thinking: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    #: What the turn consumed, in the buckets it was billed in. `tokens` is its
    #: total; this is what the cost is computed from, because output is priced
    #: several times input and a cache read a tenth of one.
    usage: TokenUsage = EMPTY_USAGE
    #: True when the run paused for tool approval rather than finishing.
    awaiting_approval: bool = False
    #: Set when the graph raised. `answer` still carries the apology chat shows
    #: the user, but a caller that records outcomes must read this instead: an
    #: agent run whose graph crashed was closed as `completed` with the apology
    #: as its answer, so every internal failure looked like a success on /runs.
    error: str = ""


async def run_tool_eagerly(
    name: str,
    arguments: dict[str, Any],
    *,
    turn: TurnContext,
    metadata: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    Run one tool before the model's first turn, applying its UI side effects.

    Used when the user has *explicitly* asked for something — `/search`,
    `/research` — where leaving it to the model to decide is a regression: they
    chose the mode, so the search must happen. Ordinary chat stays fully
    model-driven. Returns the tool output and its trace entry.
    """
    from chat import tools as tool_registry

    await turn.sink(Event.AGENT_TRACE, {
        "sub_type": "tool", "tool": name, "args": arguments, "iteration": 0,
    })

    try:
        async with asyncio.timeout(TOOL_CALL_TIMEOUT):
            output = await tool_registry.execute_chat_tool(
                name, arguments,
                {"user_id": turn.user_id, "session_id": turn.session_id,
                 "turn_id": turn.turn_id},
            )
    except asyncio.TimeoutError:
        output = f"Error: {name} timed out after {TOOL_CALL_TIMEOUT}s."
    except Exception as exc:
        logger.exception("[Tools] Eager %s raised", name)
        output = f"Error running {name}: {exc}"

    await _apply_side_effects(name, arguments, output, metadata, turn.sink)
    return output, {"tool": name, "args": arguments, "iteration": 0}


def _pending_tool_name(snapshot, call_id: str) -> str | None:
    """Which tool the paused call was for, read back out of graph state."""
    for message in reversed(snapshot.values.get("messages", [])):
        if not isinstance(message, AIMessage):
            continue
        for raw in message.tool_calls or []:
            if raw.get("id") == call_id:
                return raw.get("name")
    return None


async def approve_tool_call(
    thread_id: str, call_id: str, *, remember: bool = False,
    scope: str = "", session_key: str = "", user_id: int | None = None,
) -> None:
    """
    Record a user's approval so the paused run can resume past it.

    `scope` says how long the approval lasts, and it is the whole reason this
    is not a boolean any more:

    * `once` — this call only. The default, and the only answer that cannot be
      regretted.
    * `session` — this tool, for the rest of this conversation or run. Written
      against `ToolPermission.session_key`, a column that has existed since the
      model was added and that nothing ever wrote a non-empty value into.
    * `always` — this tool, in every conversation, until revoked.

    Before this there were two rungs, `once` and `always`, and the button
    offering the second said "remember". A user who wants to stop being asked
    about the next twenty calls in the run they are watching had no way to say
    that, so they said `always` and granted a standing allowance over their own
    mailbox to get through the afternoon. The middle rung is the one people
    actually want, and it expires on its own.

    Still keyed on the tool rather than on this call's arguments: a decision the
    user could only make about one exact argument set would never match twice,
    and they would keep answering the same prompt while believing they had
    settled it. Narrowing it further needs a way to *show* the user what they
    are agreeing to, which is a question about the prompt, not about storage.

    `remember` is the retired spelling of `scope='always'`, kept because
    `chat/turn/pipeline.py` speaks it over the wire. `scope` wins when both
    are given.

    `session_key` is what a `session`-scoped allowance is filed under, and it
    has to be passed rather than taken from `thread_id` because the two are the
    same string only *sometimes*. An agent run uses its thread id as its
    session id, so they agree; a chat turn with memory off gets a throwaway
    thread (`<id>:nomem:<uuid>`) while `TurnContext.session_id` stays the real
    session id — and `permissions.is_remembered` matches against the latter. Key
    it on the thread there and the row is written, matches nothing, and the user
    is asked again having been told they would not be.
    """
    scope = scope or ("always" if remember else "once")
    if scope not in ("once", "session", "always"):
        logger.warning("[HITL] Unknown approval scope %r; treating as once", scope)
        scope = "once"
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await get_graph().aget_state(config)
    if not snapshot.values:
        logger.warning("[HITL] No state for thread %s; approval ignored", thread_id)
        return

    meta = dict(snapshot.values.get("metadata", {}))
    approved = list(meta.get("approved_tool_calls", []))
    if call_id not in approved:
        approved.append(call_id)
    meta["approved_tool_calls"] = approved
    await get_graph().aupdate_state(config, {"metadata": meta})

    if scope == "once" or user_id is None:
        return

    tool_name = _pending_tool_name(snapshot, call_id)
    if not tool_name:
        logger.warning("[HITL] Cannot remember %s: no pending call by that id", call_id)
        return

    from chat.models import ToolPermission

    # `session` scopes the allowance to the run or conversation the user is
    # actually watching; `always` leaves the key empty, which is what
    # `permissions.is_remembered` matches in every session.
    stored_key = (session_key or thread_id)[:64] if scope == "session" else ""

    try:
        await ToolPermission.objects.aget_or_create(
            user_id=user_id, tool_name=tool_name[:160], session_key=stored_key,
        )
    except Exception:  # noqa: BLE001
        # The approval itself already landed. Failing to file it must not undo
        # the resume the user actually asked for.
        logger.exception("[HITL] Could not store the standing allowance")
    logger.info("[HITL] Approved %s on thread %s", call_id, thread_id)
    try:
        if user_id is not None:
            from logs.signals_api import arecord_signal

            await arecord_signal(user_id, 'approval_granted',
                                 detail={'tool': tool_name or '', 'scope': scope})
    except Exception:  # noqa: BLE001
        pass


async def reject_tool_call(
    thread_id: str, call_id: str, *, reason: str = "", user_id: int | None = None,
) -> bool:
    """
    Record a refusal so the paused run can resume *past* the call it asked for.

    Approval and rejection are deliberately the same mechanism — a note in
    `metadata`, written from outside the graph so it survives the interrupt's
    rollback — because they are the same decision with opposite answers. What
    they must not be is asymmetric in effect: before this existed, approving
    resumed the run and rejecting did nothing at all, so a declined call left
    the graph paused for ever. With a schedule or a trigger behind it that is a
    run that never ends and a `HITLRequest` that nudges the user for ever.

    Returns False when there is no state for the thread, so a caller can tell
    "declined" from "there was nothing to decline".
    """
    config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
    snapshot = await get_graph().aget_state(config)
    if not snapshot.values:
        logger.warning("[HITL] No state for thread %s; rejection ignored", thread_id)
        return False

    meta = dict(snapshot.values.get("metadata", {}))
    rejected = dict(meta.get("rejected_tool_calls", {}) or {})
    rejected[call_id] = reason
    meta["rejected_tool_calls"] = rejected
    await get_graph().aupdate_state(config, {"metadata": meta})
    logger.info("[HITL] Rejected %s on thread %s", call_id, thread_id)
    try:
        if user_id is not None:
            from logs.signals_api import arecord_signal

            tool_name = _pending_tool_name(snapshot, call_id) or ''
            await arecord_signal(user_id, 'approval_rejected',
                                 detail={'tool': tool_name})
    except Exception:  # noqa: BLE001
        pass
    return True


async def run_turn(
    turn: TurnContext,
    *,
    prompt: str,
    thread_id: str,
    metadata: dict[str, Any] | None = None,
    tool_trace: list[dict[str, Any]] | None = None,
) -> TurnResult:
    """
    Run the agent to completion (or to an approval pause) and return the result.

    `thread_id` keys the checkpointer. Callers wanting a turn with no recall
    should pass a throwaway id: the checkpoint holds its own copy of the
    conversation, so emptying `turn.history` alone would not stop the model
    seeing earlier turns.
    """
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id, "turn": turn},
        # LangGraph counts *node visits*, and one tool iteration is four of them
        # (agent -> tools -> curate -> steering). This was `* 2`, from before
        # curate and steering existed, so a run died with GraphRecursionError at
        # roughly half its configured iterations — before `at_limit` could
        # withhold tools and force an answer. Found by the work-tier stress
        # benchmark (2026-09-17). The final answer visit and the margin are the
        # `+ 10`.
        "recursion_limit": turn.max_iterations * STEPS_PER_ITERATION + 10,
    }
    initial: AgentState = {
        "messages": [HumanMessage(content=prompt)],
        "metadata": dict(metadata or {}),
        "tool_trace": list(tool_trace or []),
        "thinking": "",
        "total_tokens": 0,
        "usage": EMPTY_USAGE,
        "provider_error": "",
    }

    # Resume only when the graph is genuinely paused mid-run. `.values` alone is
    # the wrong test: thread_id is the session id, so every turn after the first
    # finds leftover values and would resume a finished graph — re-emitting the
    # previous answer and never reading the new message. `.next` is non-empty
    # only while nodes are still pending.
    snapshot = await get_graph().aget_state(config)
    resuming = bool(snapshot.values and snapshot.next)

    awaiting_approval = False
    try:
        final = await get_graph().ainvoke(None if resuming else initial, config=config)
    except LLMUserActionable:
        # No credential, rejected key, no credit, or a retired model. The caller
        # turns this into an error the user sees as an error; swallowing it into
        # "I hit an internal error" below would hide the one thing they can act
        # on.
        raise
    except GraphInterrupt:
        # Pre-1.0 LangGraph surfaced a pause by raising out of `ainvoke`. Kept
        # so the pause is detected under either version.
        awaiting_approval = True
        final = (await get_graph().aget_state(config)).values
    except Exception as exc:
        logger.exception("[Agent] Run failed on thread %s", thread_id)
        return TurnResult(
            answer="I hit an internal error on that turn. Please try again.",
            metadata=dict(metadata or {}),
            error=f"{type(exc).__name__}: {exc}",
        )
    else:
        # LangGraph 1.x does *not* raise: it returns the state with the pending
        # pause reported in `__interrupt__`. Detecting the pause by matching
        # "Permission required" against an exception message therefore stopped
        # working at the 1.0 upgrade, silently — the graph paused correctly and
        # `run_turn` reported the turn complete, so `run_agent` closed the log
        # as `completed`, `_find_paused_log` found nothing, and approving did
        # nothing at all. Read it from the result, which is where it now lives.
        awaiting_approval = bool(final.get("__interrupt__"))

    answer = next(
        (m.content for m in reversed(final["messages"])
         if isinstance(m, AIMessage) and m.content),
        "",
    )
    return TurnResult(
        answer=answer,
        thinking=final.get("thinking", ""),
        metadata=final.get("metadata", dict(metadata or {})),
        tool_trace=final.get("tool_trace", []),
        tokens=final.get("total_tokens", 0),
        usage=final.get("usage", EMPTY_USAGE),
        awaiting_approval=awaiting_approval,
        # Chat ignores this and shows the sentence already in `answer`; an agent
        # run raises `AgentTurnFailed` on it and is recorded as failed.
        error="" if awaiting_approval else (final.get("provider_error") or ""),
    )


async def suggest_follow_ups(
    turn: TurnContext, *, question: str, answer: str, limit: int = 3,
    usage_sink: list[TokenUsage] | None = None,
) -> list[str]:
    """
    Ask for follow-up questions in a separate, cheap call.

    Deliberately not a field on the main answer. Requiring the model to wrap a
    long markdown reply in JSON just to carry three questions is what made the
    answer impossible to stream token-by-token; this costs one small call after
    the user is already reading.

    Cheap is not free: it runs on the user's own model, so its usage is handed
    back through `usage_sink` for the turn's cost. It used to be dropped, which
    made every conversation's cost figure leave out one call per answer.
    """
    if len(answer.strip()) < 200:
        return []

    try:
        completion = await llm.complete(
            provider=turn.provider,
            model=turn.model,
            prompt=prompts.FOLLOW_UPS_TEMPLATE.format(
                question=question[:2_000], answer=answer[:6_000]
            ),
            system_message=prompts.FOLLOW_UPS_SYSTEM,
            user_id=turn.user_id,
            max_tokens=300,
            temperature=0.8,
            # Deliberately not `turn.effort`. The user chose an effort for
            # their *answer*; three follow-up questions are a formatting job,
            # and inheriting `high` here would bill a reasoning pass for them
            # after the answer is already on screen. Same call as the curation
            # fold below, for the same reason.
            effort="none",
        )
    except (LLMUnavailable, RuntimeError) as exc:
        logger.info("[FollowUps] Skipped: %s", exc)
        return []

    if usage_sink is not None:
        usage_sink.append(completion.usage)
    return _parse_follow_ups(completion.content, limit)


def _parse_follow_ups(raw: str, limit: int) -> list[str]:
    """Pull the questions out of a small JSON reply; empty list if it is junk."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []

    items = data.get("follow_ups") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [str(item).strip() for item in items if str(item).strip()][:limit]
