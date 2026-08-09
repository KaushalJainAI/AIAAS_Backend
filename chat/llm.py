"""
Provider access for the chat app.

Everything that talks to an LLM goes through `complete()` / `stream()` here:
credential resolution, the platform-key fallback, context clamping and the
provider-node lookup all live in one place so a new call site cannot forget one
of them. The node registry does the actual HTTP.

The wire format is OpenAI-shaped chat messages, which every handler in
`nodes.handlers.llm_nodes` extends verbatim into its request. That is what makes
real tool-call threading possible: an assistant message carrying `tool_calls`
and the matching `tool` results pass straight through.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable
from uuid import uuid4

from asgiref.sync import sync_to_async

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
    slug: slug
    for slug in (
        'openai', 'gemini', 'ollama', 'openrouter', 'perplexity',
        'huggingface', 'anthropic', 'deepseek', 'xai', 'nvidia',
    )
}

#: Providers whose credential slug differs from the provider slug.
_CREDENTIAL_SLUGS: dict[str, tuple[str, ...]] = {
    'gemini': ('gemini-api', 'google-oauth2'),
    'perplexity': ('perplexity-api',),
    'xai': ('xai-api',),
}

#: Platform-managed fallback keys, read from the environment. Used only when the
#: user has no personal (BYOK) credential, so the app works out of the box.
_PLATFORM_ENV_KEYS: dict[str, tuple[str, ...]] = {
    'nvidia': ('NVIDIA_API_KEY',),
    'gemini': ('GEMINI_API_KEY', 'GOOGLE_API_KEY'),
    'perplexity': ('PERPLEXITY_API_KEY',),
    'openai': ('OPENAI_API_KEY',),
    'openrouter': ('OPENROUTER_API_KEY', 'OPEN_ROUTER_KEY'),
    'anthropic': ('ANTHROPIC_API_KEY',),
    'deepseek': ('DEEPSEEK_API_KEY',),
    'xai': ('XAI_API_KEY',),
}

#: Ollama runs locally and needs no key.
_KEYLESS_PROVIDERS = frozenset({'ollama'})


class LLMUnavailable(RuntimeError):
    """No usable route to the requested provider — missing handler or credential.

    Raised rather than returned as a pseudo-answer. The previous code returned
    `{"content": "Error: ..."}`, which every caller then had to recognise by
    string-matching the content it was about to show the user; one that forgot
    displayed "Error: No verified credentials for openai" as the assistant's
    reply.
    """


# ── Token budgeting ──────────────────────────────────────────────────────────

def estimate_tokens(text: str | None) -> int:
    """Approximate token count. ~4 chars per token for English prose."""
    return len(text) // 4 if text else 0


def truncate_middle(text: str, max_tokens: int) -> str:
    """
    Cut the middle out of an oversized string, keeping both ends.

    Head-only truncation is the obvious approach and the wrong one here: the tail
    of a user turn is usually the actual question ("...given all that, which
    should I pick?"). Losing it leaves the model a pile of context and no task.
    """
    max_chars = max_tokens * 4
    if len(text) <= max_chars:
        return text
    keep = max_chars // 2
    return (
        f"{text[:keep]}\n\n"
        f"[... {len(text) - 2 * keep} characters trimmed to fit the context window ...]\n\n"
        f"{text[-keep:]}"
    )


_TRIM_NOTICE = (
    "[CONTEXT NOTICE: {n} earlier message(s) were trimmed from this window to fit "
    "the model's limit. They are still stored. If the user refers to something you "
    "cannot see, call search_conversation_history to retrieve it rather than saying "
    "you do not remember.]"
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
    only recoverable part — it stays in the DB and `search_conversation_history`
    can fetch it back. The system message and the current prompt go last,
    because dropping either changes what the model was asked to do.
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

    def history_tokens(entries: Iterable[dict]) -> int:
        return sum(
            estimate_tokens(e.get("content") if isinstance(e.get("content"), str) else "")
            for e in entries
        )

    # 2. Drop history oldest-first until the whole thing fits.
    dropped = 0
    while history and fixed_tokens + history_tokens(history) > max_total_tokens:
        history.pop(0)
        dropped += 1

    if dropped:
        logger.info(
            "[Context] Dropped %d oldest history message(s) to fit %d tokens; "
            "they remain reachable via search_conversation_history.",
            dropped, max_total_tokens,
        )
        # Without this the model cannot distinguish "never happened" from
        # "trimmed", and answers confidently from an incomplete record instead
        # of reaching for the retrieval tool.
        history.insert(0, {"role": "system", "content": _TRIM_NOTICE.format(n=dropped)})

    # 3. Still over with no history left: the prompt itself is the problem.
    if fixed_tokens > max_total_tokens:
        budget = max_total_tokens - estimate_tokens(system_message)
        prompt = truncate_middle(prompt, max(budget, 1000))
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


async def _resolve_credential(provider: str, user_id: int) -> str | None:
    """Return the id of the user's active, verified credential for `provider`."""
    from credentials.models import Credential

    slugs = _CREDENTIAL_SLUGS.get(provider, (provider,))

    @sync_to_async
    def _lookup() -> str | None:
        cred = Credential.objects.filter(
            user_id=user_id,
            credential_type__slug__in=slugs,
            is_active=True,
            is_verified=True,
        ).first()
        return str(cred.id) if cred else None

    return await _lookup()


def _platform_api_key(provider: str) -> str | None:
    """Platform default key for `provider` from the environment, if configured."""
    for env_name in _PLATFORM_ENV_KEYS.get(provider, ()):  # first match wins
        if value := os.environ.get(env_name, "").strip():
            return value
    return None


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
    from nodes.handlers.registry import get_registry

    node_type = PROVIDER_NODE_TYPES.get(provider)
    if node_type is None or not get_registry().has_handler(node_type):
        raise LLMUnavailable(f"Provider '{provider}' is not available.")

    # Applied here rather than at each call site because this is the one funnel
    # every chat LLM call goes through. Clamping per call site would mean
    # remembering it in nine places, and the one that got forgotten would be the
    # one that 400s in production.
    prompt, system_message, history = clamp_input(prompt, system_message, history)

    credential_id: str | None = None
    api_key_override: str | None = None
    if provider not in _KEYLESS_PROVIDERS:
        credential_id = await _resolve_credential(provider, user_id)
        if credential_id is None:
            api_key_override = _platform_api_key(provider)
            if api_key_override is None:
                raise LLMUnavailable(
                    f"No verified credential for '{provider}'. Add one in Settings."
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


def _execution_context(user_id: int):
    from compiler.schemas import ExecutionContext

    return ExecutionContext(execution_id=uuid4(), user_id=user_id, workflow_id=0)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Coerce a tool-call `arguments` payload into a dict."""
    import json

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
    from nodes.handlers.registry import get_registry

    request = await _build_request(
        provider=provider, model=model, prompt=prompt,
        system_message=system_message, user_id=user_id, temperature=temperature,
        max_tokens=max_tokens, tools=tools, history=history, attachments=attachments,
    )
    handler = get_registry().get_handler(request.node_type)
    result = await handler.execute({}, request.config, _execution_context(user_id))

    if not result.success:
        raise RuntimeError(f"{provider} call failed: {result.error}")

    data = (
        result.get_data() if hasattr(result, "get_data")
        else (result.items[0].json if result.items else {})
    )
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
    from nodes.handlers.registry import get_registry

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

        return kind

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
