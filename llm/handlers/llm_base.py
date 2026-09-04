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
- `ReasoningSplitter` — where reasoning ends and the answer begins, held as
                        explicit state rather than a flag juggled inside a
                        nested loop: tag vocabulary, tags torn across chunk
                        boundaries, and output that starts mid-reasoning
- `coerce_reasoning_text` — the several shapes providers put in a reasoning
                        field, flattened to text
- `ChatChunkParser`   — one chunk in, zero or more stream events out

Providers keep their own request building: the endpoints, auth, payload quirks
and media handling genuinely differ. What they no longer keep is a private
opinion about how a `<think>` tag or a malformed chunk should be handled.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Iterator, Sequence

import httpx

from ..usage import DEFAULT_CONVENTION

logger = logging.getLogger(__name__)

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"

#: Tag vocabularies that mark reasoning inside the ordinary content stream.
#: `<think>` is what DeepSeek-R1, its distills and Qwen3 emit; `<thinking>` and
#: `<reasoning>` show up in fine-tunes that imitate other vendors' formats. They
#: are matched together because a single deployment routinely serves several of
#: these models behind one OpenAI-compatible endpoint.
REASONING_TAGS: tuple[tuple[str, str], ...] = (
    (THINK_OPEN, THINK_CLOSE),
    ("<thinking>", "</thinking>"),
    ("<reasoning>", "</reasoning>"),
)
_OPEN_TAGS: tuple[str, ...] = tuple(open_ for open_, _ in REASONING_TAGS)
_CLOSE_TAGS: tuple[str, ...] = tuple(close for _, close in REASONING_TAGS)
_ANY_TAG: tuple[str, ...] = _OPEN_TAGS + _CLOSE_TAGS

# Providers disagree on which key carries chain-of-thought: OpenAI and most
# compatible vendors use `reasoning_content`, OpenRouter passes `reasoning`
# straight through from whichever upstream model produced it and additionally
# offers the structured `reasoning_details`, and Anthropic-compatible proxies
# use `thinking`. First key present wins, so the order is the preference order.
DEFAULT_REASONING_KEYS: tuple[str, ...] = (
    "reasoning_content",
    "reasoning",
    "reasoning_details",
    "thinking",
)

#: Where the text lives inside a structured reasoning entry. OpenRouter's
#: `reasoning_details` entries are `{"type": "reasoning.text", "text": ...}` or
#: `{"type": "reasoning.summary", "summary": ...}`; some proxies nest a plain
#: `{"reasoning": {"content": ...}}`.
_REASONING_TEXT_KEYS: tuple[str, ...] = (
    "text", "content", "summary", "thinking", "reasoning",
)


async def iter_sse_chunks(
    response: httpx.Response, *, allow_bare_json: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """
    Yield parsed JSON objects from an SSE stream.

    Owns the wire format so no handler has to: `data:` framing, the `[DONE]`
    terminator, comment/keep-alive lines, and chunks that do not parse. A bad
    chunk is skipped rather than fatal — a provider emitting one malformed
    frame mid-answer should cost that frame, not the response.

    `allow_bare_json` accepts lines that are JSON without a `data:` prefix, for
    endpoints that stream newline-delimited JSON instead of true SSE. It is off
    by default so a stray prose line cannot be mistaken for a chunk.
    """
    async for line in response.aiter_lines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
        elif allow_bare_json:
            payload = line
        else:
            continue
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except (ValueError, TypeError):
            logger.debug("Skipping unparseable stream chunk: %.120s", payload)
            continue


def coerce_reasoning_text(value: Any) -> str:
    """
    Flatten whatever a provider put in a reasoning field into text.

    The field is not reliably a string. OpenRouter's `reasoning_details` is a
    list of typed objects, and several proxies nest the string one level down.
    Yielding those through unchanged handed a dict to accumulators that do
    `thinking += content`, which raises TypeError and kills the stream, so the
    coercion happens here rather than at each consumer.

    Entries with no readable text — notably `reasoning.encrypted`, whose payload
    is an opaque blob — flatten to "" so they are skipped instead of being
    rendered to the user as the model's thoughts.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in _REASONING_TEXT_KEYS:
            if isinstance(text := value.get(key), str):
                return text
        return ""
    if isinstance(value, (list, tuple)):
        return "".join(coerce_reasoning_text(item) for item in value)
    return ""


def _earliest(text: str, needles: Sequence[str]) -> tuple[int, str]:
    """Index and value of whichever needle appears first in `text`."""
    found_at, found = -1, ""
    for needle in needles:
        at = text.find(needle)
        if at != -1 and (found_at == -1 or at < found_at):
            found_at, found = at, needle
    return found_at, found


def _partial_tag_len(text: str, needles: Sequence[str]) -> int:
    """
    Length of the trailing run of `text` that could still grow into a needle.

    A tag does not respect chunk boundaries: `<think>` arrives as `<thi` then
    `nk>` often enough to matter. Without this the first half is emitted as
    content and the user watches half a tag appear in the answer.
    """
    longest = max(len(needle) for needle in needles)
    for size in range(min(longest - 1, len(text)), 0, -1):
        tail = text[-size:]
        if any(needle.startswith(tail) for needle in needles):
            return size
    return 0


class ReasoningSplitter:
    """
    Routes a content stream into ("content"|"thinking", text) pairs.

    Stateful because none of the three problems it solves fit in a chunk:

    - a reasoning block spans many chunks, so `in_thinking` must carry;
    - a tag can be torn across a chunk boundary, so a partial tail is held back
      until the next chunk completes or refutes it (`flush` releases it at
      end of stream, so held text is never silently dropped);
    - a close tag with no opening tag means the output began *inside* reasoning,
      which is what the R1-distill chat templates produce by prefilling `<think>`
      into the prompt. Treating that text as content leaked the entire chain of
      thought into the answer and left a bare `</think>` in the middle of it.
      Reclassifying is only safe before any content has been emitted: past that
      point a lone close tag is a model writing about tags, not using them.
    """

    __slots__ = ("in_thinking", "_held", "_emitted_content")

    def __init__(self, in_thinking: bool = False):
        self.in_thinking = in_thinking
        self._held = ""
        self._emitted_content = False

    def feed(self, text: str) -> list[tuple[str, str]]:
        """Split one delta, returning the pairs it completes."""
        text = self._held + (text or "")
        self._held = ""
        events: list[tuple[str, str]] = []

        while text:
            if self.in_thinking:
                at, tag = _earliest(text, _CLOSE_TAGS)
                if at == -1:
                    break
                if head := text[:at]:
                    events.append(("thinking", head))
                self.in_thinking = False
            else:
                at, tag = _earliest(text, _ANY_TAG)
                if at == -1:
                    break
                head = text[:at]
                if tag in _CLOSE_TAGS:
                    # Orphan close: everything up to it was reasoning the
                    # template opened for the model. After real content has
                    # gone out, leave it alone and drop only the stray tag.
                    if head and not self._emitted_content:
                        events.append(("thinking", head))
                    elif head:
                        events.append(("content", head))
                        self._emitted_content = True
                else:
                    if head:
                        events.append(("content", head))
                        self._emitted_content = True
                    self.in_thinking = True
            text = text[at + len(tag):]

        if text:
            watching = _CLOSE_TAGS if self.in_thinking else _ANY_TAG
            keep = len(text) - _partial_tag_len(text, watching)
            self._held, text = text[keep:], text[:keep]
            if text:
                events.append(("thinking" if self.in_thinking else "content", text))
                if not self.in_thinking:
                    self._emitted_content = True

        return events

    def flush(self) -> list[tuple[str, str]]:
        """Release any held partial tag at end of stream."""
        if not self._held:
            return []
        text, self._held = self._held, ""
        return [("thinking" if self.in_thinking else "content", text)]


def split_think_tags(text: str, in_thinking: bool) -> tuple[list[tuple[str, str]], bool]:
    """
    Stateless view of `ReasoningSplitter` for callers that carry only a bool.

    Holds nothing back — a caller with nowhere to keep a partial tag is better
    served by emitting the text than by losing it — so torn tags are the one
    improvement it cannot offer. Prefer `ReasoningSplitter` in new code.
    """
    splitter = ReasoningSplitter(in_thinking)
    events = splitter.feed(text)
    events.extend(splitter.flush())
    return events, splitter.in_thinking


def _stream_error_message(error: Any) -> str:
    """The readable sentence out of an in-band stream error frame."""
    if isinstance(error, str):
        return error.strip() or "Unknown provider error"
    if isinstance(error, dict):
        for key in ("message", "detail", "error_description", "title"):
            if isinstance(value := error.get(key), str) and value.strip():
                return value.strip()
    return "Unknown provider error"


def _stream_error_status(error: Any) -> int | None:
    """The HTTP-ish status an in-band error frame reports, when it reports one.

    Kept because it is what `classify_provider_error` reads to tell a 401 the
    user must fix from a 503 they can only retry.
    """
    if not isinstance(error, dict):
        return None
    for key in ("code", "status", "status_code"):
        value = error.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


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
        usage_convention: str = DEFAULT_CONVENTION,
    ):
        self.emit_thinking = emit_thinking
        self.emit_tool_calls = emit_tool_calls
        self.reasoning_keys = tuple(reasoning_keys)
        self.splitter = ReasoningSplitter()
        #: Forwarded on every metadata frame so the accumulator can read the
        #: usage object correctly. It travels *with the usage* rather than
        #: being looked up later, because by the time a chunk reaches
        #: `StreamAccumulator` the handler that produced it is out of scope —
        #: and reading an inclusive payload as exclusive double-counts every
        #: cached token silently. See `llm/usage.py`.
        self.usage_convention = usage_convention

    def feed(self, chunk: dict[str, Any]) -> Iterator[dict[str, Any]]:
        # A provider may report a failure *inside* a 200 SSE body rather than
        # as an HTTP status: NVIDIA answers an overloaded model with
        # `data: {"error": {"message": "Service temporarily overloaded",
        # "code": 503}}` followed by `[DONE]`. Such a frame carries no
        # `choices`, so without this branch it fell through and the whole
        # stream yielded nothing -- and an empty stream is indistinguishable
        # from a model that chose to say nothing. The run was then recorded as
        # `completed` with an empty answer, which is the one failure mode that
        # never reaches whoever has to fix it. Yield it as the error frame the
        # accumulator already knows how to classify.
        if (error := chunk.get("error")) is not None:
            yield {
                "type": "error",
                "message": _stream_error_message(error),
                "status": _stream_error_status(error),
            }
            return

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
            if not delta.get(key):
                continue
            # Coerced rather than yielded raw: `reasoning_details` is a list of
            # objects, and a dict reaching a `thinking += content` accumulator
            # raises TypeError mid-stream. An entry that carries no readable
            # text coerces to "" and is dropped here.
            if (text := coerce_reasoning_text(delta[key])) and self.emit_thinking:
                yield {"type": "thinking", "content": text}
            break

        if delta.get("tool_calls") and self.emit_tool_calls:
            yield {"type": "tool_calls", "tool_calls": delta["tool_calls"]}

        if delta.get("content"):
            yield from self._emit(self.splitter.feed(delta["content"]))

        # Usage arrives on a trailing frame with empty choices when
        # stream_options.include_usage is set, and inline for other providers.
        if chunk.get("usage"):
            yield {
                "type": "metadata",
                "usage": chunk["usage"],
                "usage_convention": self.usage_convention,
            }

        if chunk.get("citations"):
            yield {"type": "citations", "citations": chunk["citations"]}

    def flush(self) -> Iterator[dict[str, Any]]:
        """
        Emit anything the splitter is still holding.

        Call once when the stream ends. The splitter withholds a trailing run
        that could still turn out to be a torn tag; without this the final
        characters of a message that happens to end in `<` never arrive.
        """
        yield from self._emit(self.splitter.flush())

    def _emit(self, events: list[tuple[str, str]]) -> Iterator[dict[str, Any]]:
        for kind, text in events:
            if kind == "thinking" and not self.emit_thinking:
                continue
            yield {"type": kind, "content": text}

    @property
    def in_thinking(self) -> bool:
        """Back-compat view of the splitter state this class used to own."""
        return self.splitter.in_thinking
