"""
Shared plumbing for chat-completion LLM providers.

Six of the nodes in `llm_nodes.py` talk to an OpenAI-compatible
`/chat/completions` endpoint, and each one carried its own copy of the same
SSE loop: split on `data: `, `json.loads` inside the loop with a fresh
`import json` every chunk, pull `choices[0].delta`, branch on
`reasoning_content` / `tool_calls` / `content`, and hand-roll an eighteen-line
`<think>` tag splitter. Five of those copies agreed. The sixth — OpenAI's — had
been mangled by an edit that left four consecutive `text = delta["content"]`
reassignments, so every chunk containing an opening `<think>` tag emitted its
leading text to the user three times.

That is the cost of the duplication rather than an argument about tidiness: a
fix applied to one copy never reached the other five, and a corruption
introduced in one copy was invisible next to five correct siblings.

This module holds that logic once:

- `iter_sse_chunks`   — the wire format: `data:` framing, `[DONE]`, bad JSON
- `ChatChunkParser`   — one chunk in, zero or more stream events out, with the
                        `<think>` split as explicit state rather than a flag
                        juggled inside a nested loop
- `dynamic_model_options` — the model-dropdown query, previously copy-pasted
                        verbatim eight times with only a slug and a default
                        differing

Providers keep their own request building: the endpoints, auth, payload quirks
and media handling genuinely differ. What they no longer keep is a private
opinion about how a `<think>` tag or a malformed chunk should be handled.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Iterator, Sequence

import httpx

logger = logging.getLogger(__name__)

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

# Providers disagree on which key carries chain-of-thought: OpenAI and most
# compatible vendors use `reasoning_content`, OpenRouter passes `reasoning`
# straight through from whichever upstream model produced it.
DEFAULT_REASONING_KEYS: tuple[str, ...] = ("reasoning_content", "reasoning")


def dynamic_model_options(slug: str, preferred: str) -> dict[str, dict[str, Any]]:
    """
    Build the `model` select-field options from the AIModel table.

    `preferred` is selected when the table offers it; otherwise the first
    registered model wins.

    Returns the empty dict both on failure and when the provider has no active
    models, rather than raising or publishing an empty dropdown. This feeds a
    form render: handing the UI `options: []` replaces a working static default
    with an unusable empty select, so "no dynamic opinion" is the safer answer.
    """
    try:
        from nodes.models import AIModel

        options = list(
            AIModel.objects.filter(provider__slug=slug, is_active=True)
            .values_list("value", flat=True)
        )
    except Exception as exc:  # noqa: BLE001 - form render must not hard-fail
        logger.warning("Failed to fetch dynamic models for %s: %s", slug, exc)
        return {}

    if not options:
        return {}

    return {
        "model": {
            "options": options,
            "defaultValue": preferred if preferred in options else options[0],
        }
    }


async def iter_sse_chunks(
    response: httpx.Response,
    *,
    allow_bare_json: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """
    Decode an SSE chat-completion stream into parsed JSON chunks.

    `allow_bare_json` accepts lines that are raw JSON objects without the
    `data: ` prefix — OpenRouter emits those when proxying certain upstreams.

    A chunk that will not parse is logged and skipped. The previous inline
    version swallowed it with a bare `except Exception: continue`, which also
    swallowed bugs in the branches below it; here only the decode is guarded.
    """
    async for line in response.aiter_lines():
        if not line:
            continue
        line = line.strip()

        if line.startswith("data: "):
            payload = line[6:].strip()
        elif allow_bare_json and line.startswith("{"):
            payload = line
        else:
            continue

        if not payload:
            continue
        if payload == "[DONE]":
            return

        try:
            chunk = json.loads(payload)
        except ValueError:
            logger.debug("Discarding unparseable stream chunk: %.200s", payload)
            continue

        if isinstance(chunk, dict):
            yield chunk


def split_think_tags(text: str, in_thinking: bool) -> tuple[list[tuple[str, str]], bool]:
    """
    Split one delta into ("content"|"thinking", text) pairs.

    Returns the pairs plus the carried `in_thinking` state, because a `<think>`
    block routinely spans many chunks. Kept a pure function so the tag grammar
    can be tested without an HTTP stream behind it.
    """
    events: list[tuple[str, str]] = []

    while text:
        if in_thinking:
            head, sep, rest = text.partition(THINK_CLOSE)
            if head:
                events.append(("thinking", head))
            if not sep:
                break
            in_thinking = False
            text = rest
        else:
            head, sep, rest = text.partition(THINK_OPEN)
            if head:
                events.append(("content", head))
            if not sep:
                break
            in_thinking = True
            text = rest

    return events, in_thinking


class ChatChunkParser:
    """
    Turns OpenAI-compatible stream chunks into the node event dicts the
    executor and WebSocket consumers already expect.

    Stateful only in `in_thinking`, which has to survive across chunks. One
    parser per stream.
    """

    def __init__(
        self,
        *,
        emit_thinking: bool = True,
        emit_tool_calls: bool = True,
        reasoning_keys: Sequence[str] = DEFAULT_REASONING_KEYS,
    ):
        self.emit_thinking = emit_thinking
        self.emit_tool_calls = emit_tool_calls
        self.reasoning_keys = tuple(reasoning_keys)
        self.in_thinking = False

    def feed(self, chunk: dict[str, Any]) -> Iterator[dict[str, Any]]:
        choices = chunk.get("choices") or []
        choice = choices[0] if choices else {}
        delta = choice.get("delta") or {}

        # Non-streaming shape: some proxies deliver a whole message in the
        # final frame instead of a delta.
        if not delta:
            message = choice.get("message") or {}
            if message.get("content"):
                delta = {"content": message["content"]}

        for key in self.reasoning_keys:
            if delta.get(key):
                if self.emit_thinking:
                    yield {"type": "thinking", "content": delta[key]}
                break

        if delta.get("tool_calls") and self.emit_tool_calls:
            yield {"type": "tool_calls", "tool_calls": delta["tool_calls"]}

        if delta.get("content"):
            events, self.in_thinking = split_think_tags(delta["content"], self.in_thinking)
            for kind, text in events:
                if kind == "thinking" and not self.emit_thinking:
                    continue
                yield {"type": kind, "content": text}

        # Usage arrives on a trailing frame with empty choices when
        # stream_options.include_usage is set, and inline for other providers.
        if chunk.get("usage"):
            yield {"type": "metadata", "usage": chunk["usage"]}

        if chunk.get("citations"):
            yield {"type": "citations", "citations": chunk["citations"]}
