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
#: an oversized tool result stored; `files` — the caller must have a virtual
#: filesystem scope, which only an agent run does.
Requirement = Literal["memory", "vision", "spill", "files"]

#: What running this tool does to the world, which is what an autonomy level
#: actually needs to know. `sensitive` answers "ask in chat"; this answers "how
#: bad is it if nobody was asked", and they are different questions — the
#: modes between `ask` and `full` are exactly the ones that need the second.
#:
#: `read` — observes only. Nothing outside this process changes, so a mode that
#:   forbids mutation can offer it and a mode that skips approvals risks
#:   nothing by running it. The sandbox counts: `execute_python` has no
#:   network, no filesystem and no imports reaching either, so what it mutates
#:   is its own dead process.
#: `reversible` — has a real side effect the user can undo without our help.
#:   The file tools qualify because a delete goes through `recycle.trash` and
#:   lands in the user's recycle bin, and a write creates a row they can trash.
#: `irreversible` — cannot be taken back. Sending an email, invoking a run that
#:   spends money, anything reaching a third party under the user's credentials.
Effect = Literal["read", "reversible", "irreversible"]

ToolFunc = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    schema: Dict[str, Any]
    run: ToolFunc
    requires: Requirement | None = None
    sensitive: bool = False
    #: May this tool run at the same time as its siblings in one turn?
    #:
    #: The model issues every call in a turn *before* seeing any result, so no
    #: call can depend on another and overlapping them is safe by
    #: construction — for tools that only read. Declared per tool rather than
    #: inferred, and defaulting to False, because the unsafe ones are unsafe
    #: for reasons a name cannot reveal: `execute_python` captures stdout with
    #: `redirect_stdout`, which swaps a process-global, so two concurrent runs
    #: interleave each other's output. MCP tool names are minted at runtime and
    #: can never carry this flag, which is the other reason the default has to
    #: be the safe one.
    parallel: bool = False

    #: What this call does to the world. Defaults to the worst case for the
    #: same reason `parallel` defaults to the safe one: an MCP tool's name is
    #: minted at runtime from a third-party catalogue and can never carry a
    #: declaration, so "undeclared" has to mean "assume the worst" or the
    #: autonomy ladder would quietly hand unknown tools the loosest treatment.
    effect: Effect = "irreversible"


_REGISTRY: Dict[str, Tool] = {}


def tool(
    schema: Dict[str, Any],
    *,
    requires: Requirement | None = None,
    sensitive: bool = False,
    parallel: bool = False,
    effect: Effect = "irreversible",
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
        _REGISTRY[name] = Tool(
            name, schema, func, requires, sensitive, parallel, effect,
        )
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


def parallel_names() -> frozenset[str]:
    """Tools that may overlap with their siblings in one turn."""
    return frozenset(t.name for t in _REGISTRY.values() if t.parallel)


def names_with_effect(*effects: Effect) -> frozenset[str]:
    """Every registered tool whose declared effect is one of `effects`.

    Returns *names*, not tools, because every caller is building a set to test
    a call's name against — an autonomy level deciding what to gate, or `plan`
    mode deciding what to offer.
    """
    wanted = frozenset(effects)
    return frozenset(t.name for t in _REGISTRY.values() if t.effect in wanted)


def effect_of(name: str) -> Effect:
    """One tool's declared effect, worst-case for anything unregistered.

    An MCP tool never appears in the registry, so it lands here and is treated
    as irreversible — which is the whole reason the default is that way round.
    """
    entry = _REGISTRY.get(name)
    return entry.effect if entry is not None else "irreversible"
