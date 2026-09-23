"""
HTTP surface for slash commands (§18.7).

Routes (all under `/api/chat/`, in `chat/urls.py`):

- `GET /api/chat/commands/` — commands this user can run, filtered the way
  `get_available_tools` filters tools.
- `GET /api/chat/commands/complete/?command=agent&arg=agent&q=rep` —
  argument candidates, computed with the same predicate the command
  validates against.
- `POST /api/chat/commands/run/` — run an action-kind command server-side
  and return a card (no model call). Turn/client commands are refused here
  with the reason: turns run through the message endpoints, clients run in
  the browser.
- `POST /api/missions/` plus list/pause/resume/cancel — the mission routes
  P7 left out; only the model could start a mission before these.

Importing this module registers every command (each domain module declares
its own with `@command(...)`, so a command's listing, completion and handler
are one block of code rather than three lists).
"""
from __future__ import annotations

import logging

from adrf.decorators import api_view as async_api_view
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from chat.commands import registry as _registry  # noqa: F401
from chat.commands import (  # noqa: F401 — registration side effect
    agents as _commands_agents,
    knowledge as _commands_knowledge,
    library as _commands_library,
    media as _commands_media,
    memory as _commands_memory,
    missions as _commands_missions,
    review as _commands_review,
    session as _commands_session,
    shortcuts as _commands_shortcuts,
    web as _commands_web,
)
from chat.commands.registry import CommandContext

logger = logging.getLogger(__name__)

#: Nothing returns an unbounded list. The catalogue is small, but a capped
#: response says so in its own body rather than looking complete.
COMMAND_LIST_LIMIT = 100


def _ctx(request, *, guest: bool = False, session_id: str = "",
         autonomy: str = "ask") -> CommandContext:
    user = getattr(request, "user", None)
    user_id = getattr(user, "id", None) or 0
    return CommandContext(
        user_id=user_id, session_id=session_id,
        autonomy=(autonomy or "ask"), guest=guest,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def command_list(request):
    """Every command this user can run, grouped for the palette / `/help`."""
    from chat.commands.resolve import visible_commands

    ctx = _ctx(request)
    commands = visible_commands(ctx)[:COMMAND_LIST_LIMIT + 1]
    truncated = len(commands) > COMMAND_LIST_LIMIT
    payload = []
    for cmd in commands[:COMMAND_LIST_LIMIT]:
        payload.append({
            "name": cmd.name, "summary": cmd.summary, "kind": cmd.kind,
            "group": cmd.group, "aliases": list(cmd.aliases),
            "requires": cmd.requires, "guest": cmd.guest,
            "args": [
                {"name": a.name, "kind": a.kind, "required": a.required,
                 "hint": a.hint,
                 **({"default": a.default} if a.default is not None else {})}
                for a in cmd.args
            ],
        })
    body: dict = {"commands": payload}
    if truncated:
        body["truncated"] = True
    return Response(body)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def command_complete(request):
    """Candidates for one argument of one command (`command`, `arg`, `q`)."""
    from asgiref.sync import async_to_sync

    from chat.commands.resolve import complete_arg

    name = str(request.query_params.get("command") or "").strip().lower().lstrip("/")
    arg_name = str(request.query_params.get("arg") or "").strip()
    query = str(request.query_params.get("q") or "")
    cmd = _registry.get(name)
    if cmd is None:
        return Response({"error": f"No command called '/{name}'."}, status=404)
    arg = next((a for a in cmd.args if a.name == arg_name), None)
    if arg is None:
        return Response(
            {"error": f"/{name} has no argument '{arg_name}'."}, status=404)
    ctx = _ctx(request)
    if ctx.guest and not cmd.guest:
        return Response({"error": "Sign in to use that command."}, status=403)
    candidates = async_to_sync(complete_arg)(cmd, arg, query, ctx)
    return Response({"command": cmd.name, "arg": arg.name,
                     "candidates": candidates})


class CommandRunSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=64)
    args = serializers.DictField(required=False, default=dict)
    #: Already-resolved chip ids from the palette (arg name -> canonical
    #: value), so the request never re-resolves a name already chosen.
    chips = serializers.DictField(required=False, default=dict)
    #: Extra confirm-sheet fields (budget, deadline, cron, timezone...).
    confirm = serializers.DictField(required=False, default=dict)


@async_api_view(["POST"])
@permission_classes([IsAuthenticated])
async def command_run(request):
    """Run an action-kind command and return its card. No model call.

    Turn commands are refused here: they run through the message endpoints
    (the resolved command becomes a trailing context message on a normal
    turn). Client commands are refused too: they run in the browser.
    """
    from chat.commands.resolve import CommandError, _requires_met

    form = CommandRunSerializer(data=request.data)
    form.is_valid(raise_exception=True)
    data = form.validated_data
    name = str(data["name"]).strip().lower().lstrip("/")
    cmd = _registry.get(name)
    if cmd is None:
        return Response({"error": f"No command called '/{name}'."}, status=404)
    ctx = _ctx(request)
    ok, reason = _requires_met(cmd, ctx)
    if not ok:
        return Response(
            {"error": reason or f"/{cmd.name} is not available right now."},
            status=400)
    if cmd.kind != "action":
        return Response(
            {"error": f"/{cmd.name} is a {cmd.kind} command: "
                      + ("send it as a chat message." if cmd.kind == "turn"
                         else "it runs in the app itself.")},
            status=400)
    from chat.commands.registry import CommandCall

    call = CommandCall(name=cmd.name, args=dict(data.get("args") or {}),
                       text="")
    try:
        result = await cmd.handler(call, ctx)
    except Exception:  # noqa: BLE001
        logger.exception("[Commands] action /%s failed", cmd.name)
        return Response({"error": f"/{cmd.name} could not run."}, status=500)
    if result.status == "error":
        return Response({"error": result.message or f"/{cmd.name} failed."},
                        status=400)
    body: dict = {"command": cmd.name, "card": result.card,
                  "message": result.message, "args": result.args}
    if result.status == "confirm":
        body["needs_confirm"] = True
    return Response(body)


class CommandConfirmSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=64)
    args = serializers.DictField(required=False, default=dict)
    confirm = serializers.DictField(required=False, default=dict)


@async_api_view(["POST"])
@permission_classes([IsAuthenticated])
async def command_confirm(request):
    """Carry out a confirm-sheet decision: starting a mission, arming a
    schedule, publishing. Pressing Start on the sheet is the approval, so
    these run without a further gate — but with the same ownership checks
    every other write path applies."""
    form = CommandConfirmSerializer(data=request.data)
    form.is_valid(raise_exception=True)
    data = form.validated_data
    name = str(data["name"]).strip().lower().lstrip("/")
    args = {**(data.get("args") or {}), **(data.get("confirm") or {})}

    if name == "goal":
        from chat.commands.missions import start_goal_mission

        try:
            payload = await start_goal_mission(user=request.user, args=args)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("[Commands] /goal confirm failed")
            return Response({"error": "The mission could not be started."},
                            status=500)
        from chat.commands.missions import _mission_card

        ctx = _ctx(request)
        card = await _mission_card(payload["mission_id"], ctx)
        return Response({"command": "goal", **payload, "card": card},
                        status=201)

    if name == "schedule":
        from chat.commands.library import create_schedule_trigger

        try:
            payload = await create_schedule_trigger(
                user=request.user, agent_id=int(args.get("agent_id")),
                cron=str(args.get("cron") or ""),
                timezone_name=str(args.get("timezone") or args.get("scheduleTimezone") or "UTC"),
                name=str(args.get("name") or ""),
            )
        except ValueError as exc:
            return Response({"error": str(exc)}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("[Commands] /schedule confirm failed")
            return Response({"error": "The schedule could not be armed."},
                            status=500)
        return Response({"command": "schedule", **payload}, status=201)

    if name == "memory_forget":
        from chat.commands.memory import memory_delete

        try:
            memory_id = int((args.get("memory_id") or 0))
        except (TypeError, ValueError):
            return Response({"error": "Pick the fact to forget."}, status=400)
        deleted = await memory_delete(user=request.user, memory_id=memory_id)
        if not deleted:
            return Response({"error": "No such memory."}, status=404)
        return Response({"command": "memory_forget", "deleted": True})

    return Response({"error": f"/{name} has nothing to confirm."}, status=400)


# ── Missions: the HTTP routes P7 left out ────────────────────────────────


class MissionCreateSerializer(serializers.Serializer):
    goal = serializers.CharField()
    agent_id = serializers.IntegerField()
    budget_inr = serializers.IntegerField(min_value=1)
    deadline_days = serializers.IntegerField(required=False, default=7,
                                             min_value=1, max_value=90)
    max_runs = serializers.IntegerField(required=False, default=20,
                                        min_value=1, max_value=100)


@async_api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
async def mission_list_create(request):
    """List the caller's missions, or start one through the same service
    `start_mission` uses. Without this route only the model could start a
    mission."""
    from asgiref.sync import sync_to_async

    from missions.models import Mission

    if request.method == "GET":
        rows = await sync_to_async(list)(
            Mission.objects.filter(user=request.user)
            .order_by("-created_at")
            .values("id", "goal", "status", "plan", "budget_inr", "spent_inr",
                    "runs_done", "max_runs", "next_wake_at", "last_report",
                    "deadline", "created_at")[:50]
        )
        out = []
        for row in rows:
            plan = row.get("plan") or []
            open_todos = [t for t in plan
                          if str((t or {}).get("status") or "open").lower()
                          not in ("done", "blocked")]
            out.append({
                **row,
                "next_wake_at": row["next_wake_at"].isoformat() if row["next_wake_at"] else None,
                "deadline": row["deadline"].isoformat() if row["deadline"] else None,
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "agent_id": await sync_to_async(
                    lambda rid=row["id"]: Mission.objects.filter(
                        id=rid).values_list("agent_id", flat=True).first())(),
                "open_todos": len(open_todos), "total_todos": len(plan),
            })
        body: dict = {"missions": out}
        return Response(body)

    form = MissionCreateSerializer(data=request.data)
    form.is_valid(raise_exception=True)
    from chat.commands.missions import start_goal_mission

    try:
        payload = await start_goal_mission(user=request.user,
                                           args=form.validated_data)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("[Missions] POST /api/missions/ failed")
        return Response({"error": "The mission could not be started."},
                        status=500)
    return Response(payload, status=status.HTTP_201_CREATED)


@async_api_view(["GET"])
@permission_classes([IsAuthenticated])
async def mission_detail(request, mission_id: int):
    from asgiref.sync import sync_to_async

    from missions.models import Mission

    row = await sync_to_async(
        lambda: Mission.objects.filter(id=mission_id, user=request.user)
        .values("id", "goal", "status", "plan", "budget_inr", "spent_inr",
                "runs_done", "max_runs", "next_wake_at", "last_report",
                "deadline", "wait_for", "notebook_path", "created_at",
                "updated_at", "agent_id").first()
    )()
    if row is None:
        return Response({"error": "Mission not found"}, status=404)
    row["next_wake_at"] = row["next_wake_at"].isoformat() if row["next_wake_at"] else None
    row["deadline"] = row["deadline"].isoformat() if row["deadline"] else None
    row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
    row["updated_at"] = row["updated_at"].isoformat() if row["updated_at"] else None
    return Response(row)


@async_api_view(["POST"])
@permission_classes([IsAuthenticated])
async def mission_action(request, mission_id: int, verb: str):
    """pause | resume | cancel one mission. Resuming re-arms `next_wake_at`
    now; pausing/cancelling leaves the chain stopped with its notebook intact."""
    from asgiref.sync import sync_to_async
    from django.utils import timezone

    from missions.models import Mission

    if verb not in ("pause", "resume", "cancel"):
        return Response({"error": "Unknown action."}, status=404)

    def _load():
        return Mission.objects.filter(id=mission_id, user=request.user).first()

    row = await sync_to_async(_load)()
    if row is None:
        return Response({"error": "Mission not found"}, status=404)
    status_map = {"pause": "paused", "resume": "active", "cancel": "cancelled"}

    def _save():
        row.status = status_map[verb]
        if verb == "resume":
            row.next_wake_at = timezone.now()
            row.save(update_fields=["status", "next_wake_at", "updated_at"])
        else:
            row.save(update_fields=["status", "updated_at"])

    await sync_to_async(_save)()
    return Response({"mission_id": row.id, "status": row.status})
