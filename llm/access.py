"""
Provider access for the chat, agent and extraction runtimes.

Everything that talks to an LLM goes through `complete()` / `stream()` here:
credential resolution, the platform-key fallback, context clamping and the
provider-handler lookup all live in one place so a new call site cannot forget
one of them. The provider registry does the actual HTTP.

This module used to be `chat/turn/llm.py`; it moved into the `llm` app with
the rest of the provider layer. Callers import it as `llm.access` (or alias it
as `llm`) — the funnel is the same either way.

The wire format is OpenAI-shaped chat messages, which every handler in
`llm.handlers.llm_nodes` extends verbatim into its request. That is what makes
real tool-call threading possible: an assistant message carrying `tool_calls`
and the matching `tool` results pass straight through.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

from . import budget
from .providers import PROVIDER_LABELS, SUPPORTED_PROVIDERS
from credentials.resolution import (
    KEYLESS_PROVIDERS,
    platform_api_key as _platform_api_key,
    resolve_credential_id as _resolve_credential,
)
from workflow_backend.thresholds import (
    MAX_LLM_INPUT_TOKENS,
    MAX_SINGLE_MESSAGE_TOKENS,
)

logger = logging.getLogger(__name__)


# ── Provider wiring ──────────────────────────────────────────────────────────

#: Provider slug → node_type in the handler registry. Identity for every current
#: provider; kept as an explicit map because the two namespaces are free to
#: diverge and an implicit passthrough would fail as a 500 rather than a 400.
PROVIDER_NODE_TYPES: dict[str, str] = {
    slug: slug for slug in SUPPORTED_PROVIDERS
}

#: Credential resolution — the slug map, the platform-key fallback and the
#: keyless-provider set all live in `credentials.resolution` now, because the
#: imagine app needs the same answers and a second copy drifted on whether an
#: unverified credential counts as usable.
_KEYLESS_PROVIDERS = KEYLESS_PROVIDERS


class LLMUnavailable(RuntimeError):
    """No usable route to the requested provider — missing handler or credential.

    Raised rather than returned as a pseudo-answer. The previous code returned
    `{"content": "Error: ..."}`, which every caller then had to recognise by
    string-matching the content it was about to show the user; one that forgot
    displayed "Error: No verified credentials for openai" as the assistant's
    reply.
    """


class LLMUserActionable(LLMUnavailable):
    """
    A provider failure the user can fix, and therefore must be *told* about.

    Separated from every other provider failure because these must never be
    dressed up as the assistant thinking or the assistant answering. Nothing
    about waiting or retrying helps: the turn is told to stop and say so, with
    a message that names what to change.
    """


class LLMAccountError(LLMUserActionable):
    """
    The account cannot pay for this call — no key, a rejected key, or no credit.

    Kept as its own branch because callers answer it differently from the other
    actionable failures: `agent_execute` maps it to 402, where a model that no
    longer exists is a 400.
    """


class LLMNoCredential(LLMAccountError):
    """Nothing configured to authenticate with."""


class LLMAccessDenied(LLMAccountError):
    """The provider rejected the key — revoked, wrong, or lacking access."""


class LLMQuotaExhausted(LLMAccountError):
    """Out of credits or over the plan's quota."""


class LLMModelUnavailable(LLMUserActionable):
    """The model is gone — retired, renamed, or not offered to this key.

    Not an account error: nothing is owed and no key is wrong. The model id is
    simply no longer one the provider serves, which the user fixes by picking
    another. Providers announce it as a 410 with an end-of-life date, or as a
    404 `model_not_found`.
    """


#: Phrases providers use for a spent balance. Checked alongside the status code
#: because 429 is shared between "slow down" (retryable, not the user's problem)
#: and "you are out of credit" (permanent until they top up), and only the body
#: separates them. OpenRouter says "insufficient credits"; OpenAI says
#: "insufficient_quota" / "billing"; NVIDIA says "credits".
_QUOTA_PHRASES = (
    "insufficient_quota",
    "insufficient credits",
    "insufficient balance",
    "exceeded your current quota",
    "quota exceeded",
    "billing",
    "payment required",
    "add credits",
    "out of credits",
    "credit balance",
)

_AUTH_PHRASES = (
    "invalid api key",
    "incorrect api key",
    "invalid_api_key",
    "unauthorized",
    "authentication",
    "no auth credentials",
)

#: How providers say "that model is not a thing any more". Models are retired
#: on a schedule, so a saved agent, a pinned default or a bookmarked session
#: goes stale on a date nobody was watching for — and the failure arrives as a
#: 410 mid-turn rather than as anything the user did.
_RETIRED_MODEL_PHRASES = (
    "end of life",
    "end-of-life",
    "no longer available",
    "no longer supported",
    "has been deprecated",
    "is deprecated",
    "model_not_found",
    "model not found",
    "unknown model",
    "does not exist",
    "is not a valid model",
)

#: Keys providers put the readable sentence under. `detail` is RFC 7807
#: (NVIDIA); `message` is OpenAI's `{"error": {"message": ...}}`; `title` is the
#: RFC 7807 short form, kept last because it is a label ("Gone") not a sentence.
_DETAIL_KEYS = ("detail", "message", "error_description", "title")


def _pluck_detail(payload: Any) -> str:
    """The human sentence inside a decoded provider error payload."""
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""
    # Nested first: OpenAI wraps the useful sentence one level down, and the
    # outer object's own `message` is usually the generic one.
    if (nested := payload.get("error")) is not None:
        if detail := _pluck_detail(nested):
            return detail
    for key in _DETAIL_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def humanize_provider_body(text: str) -> str:
    """Reduce a provider's error payload to the sentence a person can read.

    Providers answer with JSON — `{"type": "about:blank", "title": "Gone",
    "status": 410, "detail": "The model ... is no longer available."}` — and
    the whole blob was being shown to the user verbatim behind a warning
    triangle, in the position where the assistant's answer goes. Only the
    `detail` was ever worth reading; the rest is protocol noise that makes the
    message look like a crash report.

    Returns the input unchanged when there is no JSON in it, or none that
    carries a sentence, so nothing is ever lost by calling this.
    """
    raw = (text or "").strip()
    start = raw.find("{")
    if start == -1:
        return raw
    try:
        payload = json.loads(raw[start:])
    except ValueError:
        return raw
    detail = _pluck_detail(payload)
    if not detail:
        return raw
    # Whatever came before the JSON is the caller's own framing — "NVIDIA API
    # error:" — and is worth keeping in front of the sentence.
    prefix = raw[:start].strip()
    return f"{prefix} {detail}".strip() if prefix else detail


def classify_provider_error(
    status: int | None, body: str, provider: str, model: str = "",
) -> LLMUserActionable | None:
    """
    Map a provider failure onto something the user can fix, or `None`.

    `None` means "some other failure" — a timeout, a bad request, a provider
    outage — which the caller handles as before. Only the cases the user can
    act on are named here: their credentials, their balance, or the model they
    picked.
    """
    text = (body or "").lower()
    # "OpenRouter (400+ models)" is a picker label; an error message wants
    # just the name.
    label = PROVIDER_LABELS.get(provider, provider).split(" (")[0]

    if status == 402 or any(phrase in text for phrase in _QUOTA_PHRASES):
        # Deliberately not "your account": the key may be the platform's
        # shared one, in which case the user has nothing to top up and adding
        # their own is the fix. The wording covers both.
        return LLMQuotaExhausted(
            f"{label} refused the request — out of credit. Add or top up a "
            f"{label} key in Settings, or pick a model from another provider, "
            f"then send the message again."
        )

    if status in (401, 403) or any(phrase in text for phrase in _AUTH_PHRASES):
        return LLMAccessDenied(
            f"{label} rejected the API key. Check the credential in Settings — "
            f"it may have been revoked or may not cover this model."
        )

    if status == 410 or any(phrase in text for phrase in _RETIRED_MODEL_PHRASES):
        # The user picked this model, or inherited it from a default set months
        # ago; either way the fix is to pick another one, and saying so beats
        # showing them a 410 payload. The provider's own sentence carries the
        # end-of-life date, so it is kept — after ours, not instead of it.
        named = f"“{model}”" if model else "That model"
        detail = humanize_provider_body(body)
        message = (
            f"{named} is no longer available on {label}. Pick a different "
            f"model and send your message again."
        )
        return LLMModelUnavailable(f"{message} ({detail})" if detail else message)

    if status == 429:
        # Not an account error: a rate limit clears on its own.
        return None

    return None


# ── Token budgeting ──────────────────────────────────────────────────────────

#: The arithmetic itself lives in `llm.budget`, because `chat.turn.curation`
#: needs the identical numbers and the identical notion of a segment. Re-exported
#: here so the long-standing `from llm.access import estimate_tokens` imports —
#: and the reading of this module as *the* funnel — both still hold.
estimate_tokens = budget.estimate_tokens
truncate_middle = budget.truncate_middle


_TRIM_NOTICE = (
    "[CONTEXT NOTICE: {n} earlier message(s) were trimmed from this window to fit "
    "the model's limit. They are still stored. If something referred to is not "
    "visible above, retrieve it (recall_context in an agent run, "
    "search_conversation_history in chat) rather than saying you do not remember.]"
)


def clamp_input(
    prompt: str,
    system_message: str,
    history: list[dict] | None,
    max_total_tokens: int = MAX_LLM_INPUT_TOKENS,
) -> tuple[str, str, list[dict]]:
    """
    Final guard on the assembled request.

    Every other budget is computed per-section and in isolation: the history
    payload has one, tool output another, attachments a third. A turn that
    brushes all of them can still total past what the model accepts, and the
    failure mode is a hard 400 *after* the user waited through the tool calls.
    This runs on the assembled request, the first point the real total is known.

    Sacrificial order is deliberate. Old history goes first because it is the
    only recoverable part — it stays in the DB, and `search_conversation_history`
    or `recall_context` can fetch it back. The system message and the current
    prompt go last, because dropping either changes what the model was asked to
    do.

    History is dropped a *segment* at a time — an assistant tool-call turn and
    the `tool` results answering it leave together. Popping one message at a
    time, which is what this used to do, routinely left a `tool` entry whose
    `tool_call_id` no longer appeared anywhere in the request; providers answer
    that with a 400, so a long agent run did not degrade gracefully, it failed
    outright at the moment its transcript grew past the budget.
    """
    history = [dict(entry) for entry in (history or [])]

    # 1. No single message may monopolise the budget.
    system_message = truncate_middle(system_message, MAX_SINGLE_MESSAGE_TOKENS)
    prompt = truncate_middle(prompt, MAX_SINGLE_MESSAGE_TOKENS)
    for entry in history:
        content = entry.get("content") or ""
        if isinstance(content, str) and estimate_tokens(content) > MAX_SINGLE_MESSAGE_TOKENS:
            entry["content"] = truncate_middle(content, MAX_SINGLE_MESSAGE_TOKENS)

    fixed_tokens = estimate_tokens(system_message) + estimate_tokens(prompt)

    # 2. Drop history oldest-first, whole segments at a time, until it fits.
    segments = budget.split_segments(history)
    dropped = 0
    while segments and fixed_tokens + budget.history_tokens(
        budget.flatten(segments)
    ) > max_total_tokens:
        dropped += len(segments.pop(0).entries)
    history = budget.flatten(segments)

    if dropped:
        logger.info(
            "[Context] Dropped %d oldest history message(s) to fit %d tokens; "
            "they remain reachable via the retrieval tools.",
            dropped, max_total_tokens,
        )
        # Without this the model cannot distinguish "never happened" from
        # "trimmed", and answers confidently from an incomplete record instead
        # of reaching for the retrieval tool.
        history.insert(0, {"role": "system", "content": _TRIM_NOTICE.format(n=dropped)})

    # 3. Still over with no history left: the prompt itself is the problem.
    if fixed_tokens > max_total_tokens:
        room = max_total_tokens - estimate_tokens(system_message)
        prompt = truncate_middle(prompt, max(room, 1000))
        logger.warning("[Context] Prompt exceeded the input budget on its own; trimmed.")

    return prompt, system_message, history


# ── Results ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ToolCall:
    """One tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Completion:
    """A finished non-streaming model response."""

    content: str = ""
    thinking: str = ""
    #: Calls the model wants us to run. Never includes calls a handler already
    #: ran itself — see `executed_tools`.
    tool_calls: tuple[ToolCall, ...] = ()
    #: Tools a provider handler executed inside its own loop (OpenAINode does
    #: this) reported as `{"tool", "args", "result"}`. They are already done; we
    #: surface them only so the UI trace is not silently missing steps. Never
    #: re-execute these.
    executed_tools: tuple[dict[str, Any], ...] = ()
    tokens: int = 0
    media_url: str | None = None


@dataclass(slots=True)
class _Request:
    """Everything a handler needs for one call, resolved and validated."""

    node_type: str
    config: dict[str, Any]


async def _build_request(
    *,
    provider: str,
    model: str,
    prompt: str,
    system_message: str,
    user_id: int,
    temperature: float,
    max_tokens: int,
    tools: list[dict] | None,
    history: list[dict] | None,
    attachments: list | None,
) -> _Request:
    """Resolve routing + credentials and assemble the handler config."""
    from .handlers.registry import get_registry

    node_type = PROVIDER_NODE_TYPES.get(provider)
    if node_type is None or not get_registry().has_handler(node_type):
        raise LLMUnavailable(f"Provider '{provider}' is not available.")

    # Applied here rather than at each call site because this is the one funnel
    # every chat LLM call goes through. Clamping per call site would mean
    # remembering it in nine places, and the one that got forgotten would be the
    # one that 400s in production.
    #
    # The budget comes from the model rather than from a flat constant: 96k was
    # applied identically to a 200k model and an 8k one, so for the small ones
    # the guard passed and the provider rejected the request anyway. A declared
    # window can only lower the ceiling — `MAX_LLM_INPUT_TOKENS` is a cost
    # control, not a capability claim.
    # The budget comes from the model rather than from a flat constant: 96k was
    # applied identically to a 200k model and an 8k one, so for the small ones
    # the guard passed and the provider rejected the request anyway. A declared
    # window can only lower the ceiling — `MAX_LLM_INPUT_TOKENS` is a cost
    # control, not a capability claim.
    #
    # Read from cache, never queried here: this function is on every model call,
    # and an await added inside it is a suspension point in the request path.
    # `preflight` fills the cache once per turn.
    prompt, system_message, history = clamp_input(
        prompt, system_message, history,
        budget.cached_input_budget(model, reserve_output=max_tokens),
    )

    credential_id: str | None = None
    api_key_override: str | None = None
    if provider not in _KEYLESS_PROVIDERS:
        credential_id = await _resolve_credential(provider, user_id)
        if credential_id is None:
            api_key_override = _platform_api_key(provider)
            if api_key_override is None:
                raise LLMNoCredential(
                    f"No verified {PROVIDER_LABELS.get(provider, provider).split(' (')[0]}"
                    f" credential. Add one in Settings, or pick a model from a"
                    f" provider you have set up."
                )

    config: dict[str, Any] = {
        "prompt": prompt,
        "model": model,
        "system_message": system_message,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "credential": credential_id,
        "api_key_override": api_key_override,
        "history": history or [],
        "attachments": attachments or [],
    }
    if tools:
        config["tools"] = tools

    return _Request(node_type=node_type, config=config)


async def preflight(*, provider: str, model: str, user_id: int) -> None:
    """
    Check the call can be paid for *before* the turn starts working.

    `_build_request` already raises on an unroutable provider or a missing
    credential, but it does so several seconds in — after the user message is
    persisted, the history loaded and a "thinking" status pushed to the client.
    The user watches the assistant appear to work and then get an apology.

    Called at the top of a turn, this raises the same typed error while there is
    still nothing on screen, so the client shows the problem immediately.
    Nothing here calls the provider: it is a routing and credential lookup only,
    so it costs one query and cannot itself fail the turn.
    """
    from .handlers.registry import get_registry

    node_type = PROVIDER_NODE_TYPES.get(provider)
    if node_type is None or not get_registry().has_handler(node_type):
        raise LLMUnavailable(f"Provider '{provider}' is not available.")

    # Also where the model's context window is fetched and cached, so
    # `_build_request` can size the request without a query of its own. Here
    # because this already runs once per turn, before any work, and its whole
    # job is answering what has to be known before the first token. Failing to
    # learn the window is not a reason to refuse the turn — `prime` swallows its
    # own errors and the request falls back to the flat ceiling.
    await budget.prime(model)

    if provider in _KEYLESS_PROVIDERS:
        return

    if await _resolve_credential(provider, user_id) is not None:
        return
    if _platform_api_key(provider) is not None:
        return

    label = PROVIDER_LABELS.get(provider, provider).split(" (")[0]
    raise LLMNoCredential(
        f"No verified {label} credential. Add one in Settings, or pick a model "
        f"from a provider you have set up."
    )


def _execution_context(user_id: int):
    from llm.context import ExecutionContext

    return ExecutionContext(user_id=user_id)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Coerce a tool-call `arguments` payload into a dict."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("[LLM] Unparseable tool arguments: %.200s", raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def to_tool_calls(raw_calls: Iterable[dict]) -> tuple[ToolCall, ...]:
    """Normalise OpenAI-format `tool_calls` into `ToolCall` records."""
    calls: list[ToolCall] = []
    for index, raw in enumerate(raw_calls):
        function = raw.get("function") or {}
        name = (function.get("name") or "").strip()
        if not name:
            continue  # a delta fragment that never resolved to a real call
        calls.append(ToolCall(
            id=raw.get("id") or f"call_{index}",
            name=name,
            arguments=_parse_arguments(function.get("arguments")),
        ))
    return tuple(calls)


# ── Public API ───────────────────────────────────────────────────────────────

async def complete(
    *,
    provider: str,
    model: str,
    prompt: str,
    system_message: str,
    user_id: int,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tools: list[dict] | None = None,
    history: list[dict] | None = None,
    attachments: list | None = None,
) -> Completion:
    """
    Run one non-streaming completion.

    Raises `LLMUnavailable` when the provider cannot be reached at all. Provider
    errors mid-call surface as a `RuntimeError` so the caller decides what the
    user sees.
    """
    from .handlers.registry import get_registry

    request = await _build_request(
        provider=provider, model=model, prompt=prompt,
        system_message=system_message, user_id=user_id, temperature=temperature,
        max_tokens=max_tokens, tools=tools, history=history, attachments=attachments,
    )
    handler = get_registry().get_handler(request.node_type)
    result = await handler.execute({}, request.config, _execution_context(user_id))

    if not result.success:
        actionable = classify_provider_error(
            getattr(result, "status_code", None), result.error or "", provider,
            model,
        )
        if actionable is not None:
            raise actionable
        raise RuntimeError(
            f"{provider} call failed: {humanize_provider_body(result.error or '')}"
        )

    data = result.data
    usage = data.get("usage") or {}
    raw_calls = data.get("tool_calls") or []
    # Two shapes share this key. Handlers that only *report* what the model
    # asked for use OpenAI's `{"function": {...}}`; OpenAINode runs its own tool
    # loop and reports `{"tool", "args", "result"}` for calls it already made.
    # Splitting them here is what stops the agent re-running work that is done.
    pending = [c for c in raw_calls if "function" in c]
    executed = [c for c in raw_calls if "function" not in c and "tool" in c]

    return Completion(
        content=data.get("content") or "",
        thinking=data.get("thinking") or "",
        tool_calls=to_tool_calls(pending),
        executed_tools=tuple(executed),
        tokens=usage.get("total_tokens") or 0,
        media_url=data.get("media_url"),
    )


async def stream(
    *,
    provider: str,
    model: str,
    prompt: str,
    system_message: str,
    user_id: int,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tools: list[dict] | None = None,
    history: list[dict] | None = None,
    attachments: list | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    Yield raw provider chunks: `{"type": "content"|"thinking"|"tool_calls"|
    "metadata"|"error", ...}`.

    Use `StreamAccumulator` to fold these into a `Completion`.
    """
    from .handlers.registry import get_registry

    request = await _build_request(
        provider=provider, model=model, prompt=prompt,
        system_message=system_message, user_id=user_id, temperature=temperature,
        max_tokens=max_tokens, tools=tools, history=history, attachments=attachments,
    )
    handler = get_registry().get_handler(request.node_type)
    async for chunk in handler.stream_execute({}, request.config, _execution_context(user_id)):
        yield chunk


@dataclass(slots=True)
class StreamAccumulator:
    """
    Folds provider stream chunks into a `Completion`.

    Tool-call deltas arrive fragmented and indexed — name in one chunk, argument
    JSON split across several more — so they are reassembled by index before
    being parsed.
    """

    content: str = ""
    thinking: str = ""
    tokens: int = 0
    error: str | None = None
    #: HTTP status behind `error`, when the provider gave one.
    error_status: int | None = None
    _partial_calls: dict[int, dict[str, Any]] = field(default_factory=dict)

    def add(self, chunk: dict[str, Any]) -> str:
        """Fold one chunk in. Returns its chunk type."""
        kind = chunk.get("type", "")

        match kind:
            case "content":
                self.content += chunk.get("content") or ""
            case "thinking":
                self.thinking += chunk.get("content") or ""
            case "tool_calls":
                self._add_tool_call_deltas(chunk.get("tool_calls") or [])
            case "metadata":
                usage = chunk.get("usage") or {}
                self.tokens += usage.get("total_tokens") or (
                    (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
                )
            case "error":
                self.error = chunk.get("message") or "Unknown provider error"
                self.error_status = chunk.get("status")

        return kind

    def actionable_error(
        self, provider: str, model: str = "",
    ) -> LLMUserActionable | None:
        """The user-fixable problem behind this stream's error, if it was one.

        Named for what it selects rather than for billing: a retired model is
        as much the user's to fix as an empty balance, and both have to be
        raised rather than rendered as the assistant's reply.
        """
        if not self.error:
            return None
        return classify_provider_error(self.error_status, self.error, provider, model)

    def _add_tool_call_deltas(self, deltas: Iterable[dict]) -> None:
        for delta in deltas:
            index = delta.get("index", 0)
            slot = self._partial_calls.setdefault(
                index, {"id": None, "function": {"name": "", "arguments": ""}}
            )
            if delta_id := delta.get("id"):
                slot["id"] = delta_id
            function = delta.get("function") or {}
            slot["function"]["name"] += function.get("name") or ""
            slot["function"]["arguments"] += function.get("arguments") or ""

    @property
    def has_tool_calls(self) -> bool:
        return any(
            slot["function"]["name"].strip() for slot in self._partial_calls.values()
        )

    def finish(self) -> Completion:
        ordered = [self._partial_calls[i] for i in sorted(self._partial_calls)]
        return Completion(
            content=self.content,
            thinking=self.thinking,
            tool_calls=to_tool_calls(ordered),
            tokens=self.tokens,
        )