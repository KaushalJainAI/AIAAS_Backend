"""
Parsing, validation and completion for slash commands (§18.3, §18.7).

One module for all three so they cannot disagree:

- `parse_line` splits a leading `/name ...` into a command + free text. A `/`
  anywhere else is plain text (a path like `/Chat/notes.md` in a sentence
  must never trigger it).
- `complete` returns candidates for one argument, computed with the same
  predicate the handler validates against (the template rule: a picker that
  offers what the validator refuses is worse than an empty one).
- `resolve` validates a parsed line into a `CommandCall` (`{name, args,
  text}`), refusing unknown commands, unresolvable entities and missing
  requirements with a 400-quality message. It is **never sent to the model
  as plain text**: a silently un-run command reads as a command that ran.

`requires` is enforced at both doors — listing (hidden) and resolve/run
(refused) — the same both-doors rule every scope follows, because the palette
and the parser are two clients of one registry and "we didn't list it" has
never been access control.

Suggestion quality matters here: a typo (`/agent Reprter`) answers with the
closest real name ("Did you mean Reporter?"), computed by the same
`difflib`-style ranking the frontend palette uses, so the two agree on what
"close" means.
"""
from __future__ import annotations

import difflib
import json
import logging
from typing import Any

from asgiref.sync import sync_to_async

from .registry import Arg, Command, CommandContext, get as get_command

logger = logging.getLogger(__name__)

#: Payload chars kept from a typed line. A command line is one line; anything
#: longer is pasted prose that happens to start with a slash.
MAX_COMMAND_CHARS = 2000

#: Candidates returned per completion call. A person picks from these; a
#: second pagination scheme on a picker is worse than a cap.
COMPLETE_LIMIT = 20


def split_leading_command(content: str) -> tuple[str, str] | None:
    """`("/agent", "Reporter ...")` for a leading slash line, else None.

    Leading whitespace is allowed; a `/` anywhere else is plain text. Only
    the first token is the name — the rest is argument text resolved later.
    """
    text = (content or "")
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return None
    first, _, rest = stripped[1:].partition(" ")
    name = first.strip().lower()
    if not name:
        return None
    return name, rest.strip()


def _command_names() -> list[str]:
    from .registry import all_commands

    return [c.name for c in all_commands()]


def suggest_name(name: str, *, limit: int = 3) -> list[str]:
    """Closest registered command names to a typo, best first."""
    names = _command_names()
    if not names:
        return []
    return difflib.get_close_matches((name or "").lower(), names, n=limit, cutoff=0.5)


def _requires_met(cmd: Command, ctx: CommandContext) -> tuple[bool, str]:
    """Whether `cmd.requires` holds for this caller. Hidden when not."""
    need = (cmd.requires or "").strip().lower()
    if not need:
        return True, ""
    if need == "workspace":
        from workspaces.engine import workspace_available

        if workspace_available():
            return True, ""
        return False, "No workspace engine is configured on this platform."
    if need == "missions":
        # Missions are rows, not an engine: always listable for signed-in
        # users; guests never see them (guest filter applies first).
        return True, ""
    if need == "browser":
        from browsing.engine import browser_available

        if browser_available():
            return True, ""
        return False, "No browser engine is configured on this platform."
    if need in ("stt", "tts"):
        from voice.stt import stt_available
        from voice.tts import tts_available

        live = stt_available() if need == "stt" else tts_available()
        if live:
            return True, ""
        return False, "No speech engine is configured on this platform."
    if need == "esign":
        from esign.provider import esign_available

        if esign_available():
            return True, ""
        return False, "No e-signature provider is configured."
    # Unknown requirement names fail closed: a command nobody gated is a
    # command offered where it cannot run.
    return False, f"'{cmd.name}' is not available right now."


def visible_commands(ctx: CommandContext) -> list[Command]:
    """Commands this caller may see in the palette / `/help`."""
    from .registry import all_commands

    out: list[Command] = []
    for cmd in all_commands():
        if ctx.guest and not cmd.guest:
            continue
        ok, _ = _requires_met(cmd, ctx)
        if not ok:
            continue
        out.append(cmd)
    return out


# ── Completion (the picker) ──────────────────────────────────────────────


def _match(items: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """Substring match on label, best first; empty query returns the head."""
    q = (query or "").strip().lower()
    if not q:
        return items[:COMPLETE_LIMIT]
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        label = str(item.get("label") or item.get("name") or "").lower()
        if q in label:
            # Prefix matches first, then shorter labels (closer to the query).
            scored.append((0 if label.startswith(q) else 1, item))
    scored.sort(key=lambda pair: (pair[0], len(str(pair[1].get("label") or ""))))
    return [item for _, item in scored[:COMPLETE_LIMIT]]


async def complete_arg(
    cmd: Command, arg: Arg, query: str, ctx: CommandContext
) -> list[dict[str, Any]]:
    """Candidates for one argument, validated by the same predicate the
    handler resolves with."""
    kind = arg.kind
    if kind == "agent":
        return _match(await _agent_candidates(ctx), query)
    if kind == "skill":
        return _match(await _skill_candidates(ctx), query)
    if kind == "model":
        return _match(await _model_candidates(ctx), query)
    if kind == "effort":
        from llm.effort import LADDER

        return _match(
            [{"value": level, "label": level} for level in LADDER], query
        )
    if kind == "file":
        return _match(await _file_candidates(ctx, query), query)
    if kind == "project":
        return _match(await _project_candidates(ctx), query)
    if kind == "connection":
        return _match(await _connection_candidates(ctx), query)
    if kind == "mission":
        return _match(await _mission_candidates(ctx), query)
    if kind == "visibility":
        return _match(
            [
                {"value": "link", "label": "link — anyone with the link"},
                {"value": "platform", "label": "platform — everyone here"},
                {"value": "public", "label": "public — anyone on the internet"},
            ],
            query,
        )
    return []


@sync_to_async
def _agent_candidates(ctx: CommandContext) -> list[dict[str, Any]]:
    from agents.models import SubAgent

    rows = list(
        SubAgent.objects.filter(user_id=ctx.user_id)
        .exclude(status="archived")
        .order_by("name")
        .values("id", "name", "description")[:200]
    )
    return [
        {
            "id": r["id"], "value": str(r["id"]), "label": r["name"],
            "description": (r.get("description") or "")[:200],
        }
        for r in rows
    ]


@sync_to_async
def _skill_candidates(ctx: CommandContext) -> list[dict[str, Any]]:
    from django.db.models import Q

    from skills.models import Skill

    rows = list(
        Skill.objects.filter(Q(user_id=ctx.user_id) | Q(is_shared=True))
        .order_by("title")
        .values("id", "title")[:200]
    )
    return [
        {"id": r["id"], "value": str(r["id"]), "label": r["title"]}
        for r in rows
    ]


@sync_to_async
def _model_candidates(ctx: CommandContext) -> list[dict[str, Any]]:
    from llm.effort import clean_levels
    from llm.models import AIProvider
    from llm.providers import SUPPORTED_PROVIDERS

    out: list[dict[str, Any]] = []
    providers = list(
        AIProvider.objects.filter(is_active=True, slug__in=SUPPORTED_PROVIDERS)
        .prefetch_related("models")
    )
    for provider in providers:
        for model in provider.models.filter(is_active=True).order_by("name"):
            out.append({
                "value": model.value,
                "label": f"{model.name} ({provider.name})",
                "provider": provider.slug,
                "effort_levels": list(clean_levels(model.effort_levels)),
            })
            if len(out) >= 200:
                return out
    return out


@sync_to_async
def _file_candidates(ctx: CommandContext, query: str) -> list[dict[str, Any]]:
    """VFS paths matching a substring, inside the caller's chat scope."""
    from django.contrib.auth import get_user_model

    from inference import vfs

    try:
        user = get_user_model().objects.filter(id=ctx.user_id).first()
        if user is None:
            return []
        scope = vfs.chat_scope(user)
        found = vfs.find(scope, (query or "").strip() or "/", limit=COMPLETE_LIMIT)
        items = found.get("matches") or found.get("files") or []
        out = []
        for entry in items if isinstance(items, list) else []:
            path = entry.get("path") if isinstance(entry, dict) else str(entry)
            if path:
                out.append({"value": str(path), "label": str(path)})
        return out
    except Exception:  # noqa: BLE001 — completion must not fail a keystroke
        logger.warning("[Commands] File completion failed", exc_info=True)
        return []


@sync_to_async
def _project_candidates(ctx: CommandContext) -> list[dict[str, Any]]:
    from workspaces.models import CodeProject

    rows = list(
        CodeProject.objects.filter(user_id=ctx.user_id)
        .order_by("name")
        .values("id", "name")[:200]
    )
    return [
        {"id": r["id"], "value": r["name"], "label": r["name"]} for r in rows
    ]


@sync_to_async
def _connection_candidates(ctx: CommandContext) -> list[dict[str, Any]]:
    from mcp_integration.client import visible_servers_sync

    try:
        servers = visible_servers_sync(ctx.user_id)
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "id": s.id, "value": str(s.id),
            "label": getattr(s, "label", None) or getattr(s, "name", f"connection {s.id}"),
        }
        for s in servers
    ]


@sync_to_async
def _mission_candidates(ctx: CommandContext) -> list[dict[str, Any]]:
    from missions.models import Mission

    rows = list(
        Mission.objects.filter(user_id=ctx.user_id)
        .order_by("-created_at")
        .values("id", "goal", "status")[:50]
    )
    return [
        {
            "id": r["id"], "value": str(r["id"]),
            "label": f"#{r['id']} {str(r['goal'])[:60]} ({r['status']})",
        }
        for r in rows
    ]


# ── Resolution (the parser) ──────────────────────────────────────────────


class CommandError(ValueError):
    """A command line that cannot run. The message is written for the user."""


async def resolve_arg_value(arg: Arg, raw: str, ctx: CommandContext) -> Any:
    """Validate one raw argument token against the same predicate completion
    offers from. IDs pass through; names resolve case-insensitively."""
    text = (raw or "").strip()
    if not text:
        if arg.required:
            raise CommandError(f"'{arg.name}' is required.")
        return arg.default
    kind = arg.kind
    if kind in ("agent", "skill", "project", "mission", "connection"):
        resolved = await _resolve_entity(kind, text, ctx)
        if resolved is None:
            raise CommandError(_unknown_entity_message(kind, text, ctx))
        return resolved
    if kind == "model":
        value = await _resolve_model(text, ctx)
        if value is None:
            raise CommandError(
                f"No model called '{text}'. Pick one from the model list."
            )
        return value
    if kind == "effort":
        from llm.effort import normalize

        level = normalize(text)
        if level is None:
            raise CommandError(
                f"'{text}' is not a reasoning effort level "
                "(none, minimal, low, medium, high)."
            )
        return level
    if kind == "duration":
        return _parse_duration(text)
    if kind == "visibility":
        value = text.strip().lower()
        if value not in ("link", "platform", "public"):
            raise CommandError("Visibility is link, platform or public.")
        return value
    return text


async def _resolve_entity(kind: str, text: str, ctx: CommandContext) -> Any:
    """An id or a case-insensitive name to its canonical value, or None."""
    if kind == "agent":
        return await _resolve_agent(text, ctx)
    if kind == "skill":
        return await _resolve_skill(text, ctx)
    if kind == "project":
        return await _resolve_project(text, ctx)
    if kind == "mission":
        return await _resolve_mission(text, ctx)
    if kind == "connection":
        return await _resolve_connection(text, ctx)
    return None


@sync_to_async
def _resolve_agent(text: str, ctx: CommandContext):
    from agents.models import SubAgent

    try:
        wanted = int(text)
        row = SubAgent.objects.filter(id=wanted, user_id=ctx.user_id).first()
        return row.id if row else None
    except (TypeError, ValueError):
        pass
    row = SubAgent.objects.filter(
        user_id=ctx.user_id, name__iexact=text.strip()
    ).exclude(status="archived").first()
    return row.id if row else None


@sync_to_async
def _resolve_skill(text: str, ctx: CommandContext):
    from django.db.models import Q

    from skills.models import Skill

    try:
        wanted = int(text)
        row = Skill.objects.filter(
            Q(user_id=ctx.user_id) | Q(is_shared=True), id=wanted
        ).first()
        return row.id if row else None
    except (TypeError, ValueError):
        pass
    row = Skill.objects.filter(
        Q(user_id=ctx.user_id) | Q(is_shared=True), title__iexact=text.strip()
    ).first()
    return row.id if row else None


@sync_to_async
def _resolve_project(text: str, ctx: CommandContext):
    from workspaces.models import CodeProject

    try:
        wanted = int(text)
        row = CodeProject.objects.filter(id=wanted, user_id=ctx.user_id).first()
        return row.name if row else None
    except (TypeError, ValueError):
        pass
    row = CodeProject.objects.filter(
        user_id=ctx.user_id, name__iexact=text.strip()
    ).first()
    return row.name if row else None


@sync_to_async
def _resolve_mission(text: str, ctx: CommandContext):
    from missions.models import Mission

    try:
        wanted = int(str(text).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None
    row = Mission.objects.filter(id=wanted, user_id=ctx.user_id).first()
    return row.id if row else None


@sync_to_async
def _resolve_connection(text: str, ctx: CommandContext):
    from mcp_integration.client import visible_servers_sync

    try:
        servers = visible_servers_sync(ctx.user_id)
    except Exception:  # noqa: BLE001
        return None
    needle = text.strip().lower()
    for server in servers:
        if str(server.id) == needle:
            return server.id
        label = (getattr(server, "label", None) or getattr(server, "name", "") or "").lower()
        if label and label == needle:
            return server.id
    return None


@sync_to_async
def _resolve_model(text: str, ctx: CommandContext):
    from llm.models import AIModel

    needle = text.strip()
    row = AIModel.objects.filter(value__iexact=needle, is_active=True).first()
    if row:
        return row.value
    row = AIModel.objects.filter(name__iexact=needle, is_active=True).first()
    return row.value if row else None


def _unknown_entity_message(kind: str, text: str, ctx: CommandContext) -> str:
    nouns = {
        "agent": "agent", "skill": "skill", "project": "project",
        "mission": "mission", "connection": "connection",
    }
    noun = nouns.get(kind, "item")
    return (
        f"No {noun} called '{text}'. Pick one from the list — "
        f"a name the completer never offered is not one that can run."
    )


def _parse_duration(text: str) -> int:
    """`90m`, `2h`, `3d` or bare minutes → seconds. Refused, not guessed."""
    raw = (text or "").strip().lower()
    if not raw:
        raise CommandError("Give a duration like 90m, 2h or 3d.")
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if raw[-1] in multipliers and raw[:-1].strip().isdigit():
        return int(raw[:-1].strip()) * multipliers[raw[-1]]
    if raw.isdigit():
        return int(raw) * 60
    raise CommandError(f"'{text}' is not a duration. Use 90m, 2h or 3d.")


async def resolve_line(
    content: str, ctx: CommandContext, *, chips: dict[str, Any] | None = None
) -> tuple[Command, dict[str, Any], str]:
    """Parse + validate a typed command line into `(command, args, text)`.

    `chips` carries already-resolved entity ids from the palette (chip id →
    canonical value), so the request never re-resolves a name the user already
    chose. Anything that fails to parse or resolve raises `CommandError`,
    which the caller renders as a 400 under the input with the text left in
    place — never sent to the model as plain text.
    """
    if len(content or "") > MAX_COMMAND_CHARS:
        raise CommandError("That command line is too long to be a command.")
    split = split_leading_command(content)
    if split is None:
        raise CommandError("Not a command. Commands start with / at the line start.")
    name, rest = split
    cmd = get_command(name)
    if cmd is None:
        suggestions = suggest_name(name)
        hint = f" Did you mean /{suggestions[0]}?" if suggestions else ""
        raise CommandError(f"No command called '/{name}'.{hint} Try /help.")
    if ctx.guest and not cmd.guest:
        raise CommandError(f"/{cmd.name} needs a signed-in account.")
    ok, reason = _requires_met(cmd, ctx)
    if not ok:
        raise CommandError(reason or f"/{cmd.name} is not available right now.")

    chips = chips or {}
    args: dict[str, Any] = {}
    remaining = rest
    declared = list(cmd.args)
    if not declared:
        return cmd, args, remaining

    # A chip-resolved entity is consumed as the argument, never re-resolved:
    # the palette already validated it, and a name the user chose must not be
    # parsed again (the template rule, applied to our own picker).
    for arg in declared:
        if arg.name in chips and arg.kind in (
            "agent", "skill", "project", "mission", "model", "connection",
        ):
            args[arg.name] = chips[arg.name]
    # One entity-leading argument may be taken positionally from the head of
    # the line (`/agent Reporter summarise inbox`); the rest of the line is
    # the free text that argument type leaves behind. A head token that
    # resolves to nothing is refused here — never passed through as free
    # text — so `/agent Reprter hi` is "No agent called 'Reprter'", not a
    # run of some other agent with a mangled task.
    first = declared[0]
    if first.name not in args and first.kind in (
        "agent", "skill", "project", "mission", "model", "connection",
    ):
        head, _, tail = remaining.partition(" ")
        candidate = head.strip()
        if candidate:
            resolved = await _resolve_entity(first.kind, candidate, ctx)
            if resolved is not None:
                args[first.name] = resolved
                remaining = tail.strip()
            elif first.required:
                raise CommandError(
                    _unknown_entity_message(first.kind, candidate, ctx))
    text_args = [a for a in declared if a.kind == "text"]
    first_text = text_args[0] if text_args else None
    for arg in declared:
        if arg.name in args:
            continue
        if arg.name in chips:
            args[arg.name] = chips[arg.name]
        elif arg.kind == "text" and arg is first_text and remaining:
            # The trailing free text fills the first text argument — required
            # or not (`/research <question>`, `/goal <text>`, the task tail of
            # `/agent Name <task>`). It is consumed (not merely copied) so the
            # leftover `remaining` is what the handler did not take — empty
            # for a fully-parsed line. A second text argument never takes it.
            args[arg.name] = remaining
            remaining = ""
        elif arg.required and arg.name not in args:
            # An empty trailing text is not a refusal for a text argument:
            # the handler asks for it (`/agent` with no task). Required
            # *entity* arguments already refused above.
            if arg.kind == "text":
                continue
            raise CommandError(f"'{arg.name}' is required. {arg.hint}".strip())
        elif arg.default is not None:
            args[arg.name] = arg.default
    return cmd, args, remaining


def parse_command_payload(payload: Any) -> dict[str, Any] | None:
    """Read `TurnRequest.command` off a request body, or None when absent."""
    if not isinstance(payload, dict):
        return None
    raw = payload.get("command")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CommandError("'command' must be an object.")
    name = str(raw.get("name") or "").strip().lower().lstrip("/")
    if not name:
        raise CommandError("The command has no name.")
    args = raw.get("args") or {}
    if not isinstance(args, dict):
        raise CommandError("The command args must be an object.")
    text = str(raw.get("text") or "")
    return {"name": name, "args": args, "text": text}


def command_json(payload: Any) -> str:
    """Stable JSON for `metadata.command`, so regenerate replays the same call."""
    return json.dumps(payload or {}, sort_keys=True, default=str)
