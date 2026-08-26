"""
The tool registry: one declaration per tool, schema and implementation together.

A tool used to be defined in three places — its JSON schema in one list, its
implementation eight hundred lines away, and its name a third time in a
dispatch dict. Nothing tied them, so a schema with no dispatch entry advertised
a tool that answered "not recognized", and only a test that scraped the source
of `execute` could notice. Here the decorator *is* the registration: a tool
that is advertised is dispatchable because it is the same object, and one that
is never imported is neither.

Availability is declared the same way. `requires` names the precondition the
tool cannot run without — a witness, a memory, a spilled output — so the
condition sits on the tool instead of in a filter that matches on strings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterator, Literal

#: What a tool needs before it may be offered. `None` means always available.
#: `memory` — the conversation must have history to consult; `vision` — a
#: vision witness must resolve for this user; `spill` — this session must have
#: an oversized tool result stored.
Requirement = Literal["memory", "vision", "spill"]

ToolFunc = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    schema: Dict[str, Any]
    run: ToolFunc
    requires: Requirement | None = None
    sensitive: bool = False


_REGISTRY: Dict[str, Tool] = {}


def tool(
    schema: Dict[str, Any],
    *,
    requires: Requirement | None = None,
    sensitive: bool = False,
) -> Callable[[ToolFunc], ToolFunc]:
    """
    Register a tool from its own schema and return the function unchanged.

    Unchanged on purpose: the function stays directly callable and directly
    testable, so registration adds a lookup without adding a wrapper to debug
    through.
    """
    name = schema["function"]["name"]

    def register(func: ToolFunc) -> ToolFunc:
        if name in _REGISTRY:
            raise RuntimeError(
                f"Tool {name!r} is registered twice — two modules claim the same name."
            )
        _REGISTRY[name] = Tool(name, schema, func, requires, sensitive)
        return func

    return register


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def all_tools() -> Iterator[Tool]:
    """Every registered tool, in the order its module was imported."""
    return iter(_REGISTRY.values())


def schemas() -> list[Dict[str, Any]]:
    """Every tool's schema, ungated — the full catalogue, not one user's view."""
    return [t.schema for t in _REGISTRY.values()]


def sensitive_names() -> list[str]:
    return [t.name for t in _REGISTRY.values() if t.sensitive]
