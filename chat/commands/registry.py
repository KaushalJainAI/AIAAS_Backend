"""
The command registry: one declaration per slash command, schema and handler together.

Mirrors `chat/tools/registry.py` exactly: the decorator *is* the
registration, so a command that is listed is dispatchable because it is the
same object. `GET /api/chat/commands/` lists what this user can run (filtered
the way `get_available_tools` filters tools: an engine set to `none` or a
missing grant hides the command rather than showing one that refuses).
`GET /api/chat/commands/complete/` returns argument candidates computed with
the same predicate the command validates against (the template rule: a picker
that offers what the validator refuses is worse than an empty one).

Three kinds (§18.2):

- **client**: handled entirely in the browser (a setting, a panel). Never hits
  the backend; listed here so `/help` is complete and one registry answers
  "what can I type".
- **action**: `POST /api/chat/commands/run/` does one thing server-side and
  returns a card. No model call.
- **turn**: starts a normal chat turn. The resolved command becomes a trailing
  context message for that turn (never the system prompt: that is the clock
  trap) and may pin an intent or narrow the toolbox.

Every turn command is stored on the user message
(`metadata.command = {name, args}`), so the transcript shows a chip rather
than expanded text, and a regenerate replays exactly the same command.

Consent rule: a command the user typed is their own instruction. That covers
the **first** action it names (`/agent Reporter ...` starts the run without
the `run_agent` approval card, just as the Run button on `/agents` does). It
covers nothing after that. Every tool call the model or the started agent
then makes is gated by the usual autonomy, grants, scopes and
`toolPermissions`. Commands whose first action spends money or leaves the
platform open a **confirm sheet** that shows the resolved arguments; pressing
Start on that sheet is the approval.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal


#: A command's argument kinds, mirroring §18.7 (`agent | skill | file | model |
#: effort | connection | project | text | duration`).
ArgKind = Literal[
    "agent", "skill", "file", "model", "effort", "connection", "project",
    "text", "duration", "visibility", "mission", "schedule",
]

#: What happens when the command is invoked.
CommandKind = Literal["client", "action", "turn"]


@dataclass(frozen=True, slots=True)
class Arg:
    """One named argument a command takes."""

    name: str
    kind: ArgKind = "text"
    required: bool = False
    #: One-line hint shown in the palette / confirm sheet.
    hint: str = ""
    #: Default when the user does not supply one.
    default: Any = None


@dataclass(frozen=True, slots=True)
class CommandCall:
    """A resolved invocation: the arguments plus the leftover free text."""

    name: str
    args: dict[str, Any]
    text: str = ""


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Who is asking, and in which conversation."""

    user_id: int
    session_id: str = ""
    #: Chat autonomy level (`ask | auto | plan`), read so `/agent` can refuse
    #: in `plan` mode (starting a run is not a read).
    autonomy: str = "ask"
    #: Whether this is the guest surface (only `guest=True` commands list).
    guest: bool = False


@dataclass(frozen=True, slots=True)
class CommandResult:
    """What a handler decided."""

    #: `ok` — resolved and ready; `confirm` — needs the confirm sheet first;
    #: `error` — refused, with a message written for the user.
    status: str
    #: Human-readable message for `error`, or the sheet subtitle for `confirm`.
    message: str = ""
    #: The validated arguments, echoed back so the client can render the sheet.
    args: dict[str, Any] = field(default_factory=dict)
    #: For turn commands: the trailing context block for this turn (never the
    #: system prompt), plus optional intent/toolbox pins.
    context_block: str = ""
    intent: str = ""
    #: Tool names to pin for this turn (e.g. office tools for `/deck`), or
    #: None to leave the toolbox alone.
    tool_pin: tuple[str, ...] | None = None
    #: For action commands: the card payload the client renders.
    card: dict[str, Any] = field(default_factory=dict)
    #: True when the command starts a run directly (consent covers the first
    #: action, §18.2). Carries `{"agent_id", "goal"}` for `/agent`, or the
    #: mission payload for `/goal`.
    start: dict[str, Any] = field(default_factory=dict)


CommandFunc = Callable[[CommandCall, CommandContext], Awaitable[CommandResult]]


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    summary: str
    args: tuple[Arg, ...]
    kind: CommandKind
    handler: CommandFunc
    #: Requirement a caller must meet before this is listed (e.g.
    #: `"workspace"`, `"missions"`): hidden when unmet, never
    #: offered-then-refusing (the one-door rule).
    requires: str | None = None
    #: Guests see only these (`/help`, `/new`, `/research`).
    guest: bool = False
    #: Group shown in the palette / `/help` (agents, goals, memory, ...).
    group: str = ""
    #: Slash aliases (`clear` for `new`).
    aliases: tuple[str, ...] = ()


_REGISTRY: dict[str, Command] = {}
_ALIASES: dict[str, str] = {}


def command(
    *,
    name: str,
    summary: str,
    args: list[Arg] | None = None,
    kind: CommandKind = "action",
    requires: str | None = None,
    guest: bool = False,
    group: str = "",
    aliases: list[str] | None = None,
):
    """Register a slash command from its own declaration. Returns the function."""

    def register(func: CommandFunc) -> CommandFunc:
        key = (name or "").strip().lower().lstrip("/")
        if not key:
            raise RuntimeError("A command needs a name.")
        if key in _REGISTRY:
            raise RuntimeError(
                f"Command {key!r} is registered twice."
            )
        entry = Command(
            name=key,
            summary=summary,
            args=tuple(args or ()),
            kind=kind,
            handler=func,
            requires=requires,
            guest=guest,
            group=group or "general",
            aliases=tuple(a.strip().lower().lstrip("/") for a in (aliases or ()) if a.strip()),
        )
        _REGISTRY[key] = entry
        for alias in entry.aliases:
            if alias in _REGISTRY or alias in _ALIASES:
                raise RuntimeError(f"Command alias {alias!r} collides.")
            _ALIASES[alias] = key
        return func

    return register


def get(name: str) -> Command | None:
    """The command behind `/name` or one of its aliases, or None."""
    key = (name or "").strip().lower().lstrip("/")
    if not key:
        return None
    if key in _REGISTRY:
        return _REGISTRY[key]
    target = _ALIASES.get(key)
    return _REGISTRY.get(target) if target else None


def all_commands() -> list[Command]:
    """Every registered command, in registration order."""
    return list(_REGISTRY.values())


def listing() -> list[dict[str, Any]]:
    """Every command as the palette / `/help` renders it (unguarded)."""
    return [
        {
            "name": c.name,
            "summary": c.summary,
            "kind": c.kind,
            "group": c.group,
            "aliases": list(c.aliases),
            "requires": c.requires,
            "guest": c.guest,
            "args": [
                {
                    "name": a.name, "kind": a.kind, "required": a.required,
                    "hint": a.hint,
                    **({"default": a.default} if a.default is not None else {}),
                }
                for a in c.args
            ],
        }
        for c in _REGISTRY.values()
    ]


@dataclass(frozen=True, slots=True)
class ResolvedCommand:
    """A parsed `/name ...` line: which command, which args, what is left."""

    command: Command
    call: CommandCall
