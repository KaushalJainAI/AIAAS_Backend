"""
Fallback parser for models that write tool calls as text instead of emitting
native `tool_calls`.

This used to be a 480-line scraper with twenty regexes covering a dozen invented
dialects — ReAct prose, `<|python_tag|>`, `minimax:tool_call`, arrow-hashes and
so on. Almost all of that was compensating for `agent.py` flattening tool
results into prose, which took models off-distribution and made them imitate
tool syntax rather than emit it. With the transcript threaded properly, capable
models never land here.

What remains covers small local models (Ollama and similar), which do reliably
produce one of two shapes. Anything more exotic is not worth guessing at: a
wrong guess runs the wrong tool, which is worse than telling the model its call
was not understood and letting it retry.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator

from llm.access import ToolCall

logger = logging.getLogger(__name__)

#: Explicitly delimited call: [TOOL_CALL]{"tool": "...", "args": {...}}[/TOOL_CALL]
_DELIMITED = re.compile(r"\[TOOL_CALL\](.*?)(?:\[/TOOL_CALL\]|$)", re.DOTALL)

_NAME_KEYS = ("tool", "name", "tool_name", "function")
_ARG_KEYS = ("args", "arguments", "parameters", "params")


def _iter_json_objects(text: str) -> Iterator[tuple[dict[str, Any], str]]:
    """Yield every balanced JSON object embedded in `text`, with its raw slice."""
    decoder = json.JSONDecoder()
    index = 0
    while (start := text.find("{", index)) != -1:
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            yield value, text[start:start + end]
        index = start + max(end, 1)


def _as_tool_call(payload: dict[str, Any], index: int) -> ToolCall | None:
    """Interpret a decoded object as a tool call, or return None."""
    name = next(
        (payload[key] for key in _NAME_KEYS if isinstance(payload.get(key), str)),
        None,
    )
    if not name:
        return None

    arguments = next(
        (payload[key] for key in _ARG_KEYS if isinstance(payload.get(key), dict)),
        None,
    )
    if arguments is None:
        # Some models inline the arguments alongside the name rather than
        # nesting them. Treat every other key as an argument.
        arguments = {
            k: v for k, v in payload.items()
            if k not in _NAME_KEYS and k not in _ARG_KEYS
        }

    return ToolCall(id=f"text_call_{index}", name=name.strip(), arguments=arguments)


def extract_text_tool_calls(text: str) -> tuple[ToolCall, ...]:
    """Parse tool calls a model wrote into its message text."""
    return tuple(call for call, _ in _find(text))


def split_text_tool_calls(text: str) -> tuple[tuple[ToolCall, ...], str]:
    """
    Return the tool calls found in `text` and `text` with them removed.

    Leaving the raw call in the message body would show the user the model's
    internal syntax, so the two always travel together.
    """
    found = _find(text)
    remaining = text
    for _, raw in found:
        remaining = remaining.replace(raw, "")
    return tuple(call for call, _ in found), _tidy(remaining)


def _find(text: str) -> list[tuple[ToolCall, str]]:
    if not text or "{" not in text:
        return []

    found: list[tuple[ToolCall, str]] = []
    consumed: list[str] = []

    for match in _DELIMITED.finditer(text):
        for payload, _ in _iter_json_objects(match.group(1)):
            if call := _as_tool_call(payload, len(found)):
                found.append((call, match.group(0)))
                consumed.append(match.group(0))
                break

    remainder = text
    for block in consumed:
        remainder = remainder.replace(block, "")

    for payload, raw in _iter_json_objects(remainder):
        if call := _as_tool_call(payload, len(found)):
            found.append((call, raw))

    if found:
        logger.debug("[Extraction] Recovered %d text-form call(s)", len(found))
    return found


def _tidy(text: str) -> str:
    """Clean up the empty fences and blank runs left behind by removal."""
    text = re.sub(r"```(?:json|tool_code)?\s*```", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
