"""
Model calls a chat turn makes that are not the turn's own loop.

The turn's cost is priced from the usage its graph accumulates. A call made
*inside a tool* on another model — the vision witness answering `ask_vision` —
never reaches that state, so it was paid for and left out of the figure in the
chat header. Threading a sink through `tools_node` into every tool would be a
parameter on every tool for the sake of one; a context variable set for the
duration of the turn reaches the witness without any of them knowing.

Why a context variable holding a *list*: `tools_node` gathers safe tools as
separate tasks, and a task gets a copy of the context — but a copy of a
mapping that points at the same list, so a call recorded in a child task is
visible to the turn that set it. Outside a turn (an agent run, a test) nothing
is set and recording is a no-op.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from llm.usage import TokenUsage

_calls: ContextVar[list | None] = ContextVar('chat_side_calls', default=None)


@dataclass(frozen=True)
class SideCall:
    model: str
    usage: TokenUsage


def start() -> list[SideCall]:
    """Begin collecting for this turn. Returns the list calls land in."""
    calls: list[SideCall] = []
    _calls.set(calls)
    return calls


def record(model: str, usage: TokenUsage | None) -> None:
    """Note one side call's usage, if a turn is collecting and it used any."""
    calls = _calls.get()
    if calls is None or usage is None or usage.is_empty:
        return
    calls.append(SideCall(model=model, usage=usage))


def collected() -> list[SideCall]:
    """What this turn has collected so far (empty outside a turn)."""
    return list(_calls.get() or ())
