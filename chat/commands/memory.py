"""
`/memory` — the UI over `core.UserMemory` (§18.5).

`/memory` opens a panel listing facts by category, with edit and delete (a
client command backed by the existing `UserMemoryView`). `/memory <fact>` is
an action that writes through `core/memory.py`, the same door the tool uses,
so dedup-as-touch, per-category caps and eviction still apply. No model
call. `/memory forget <text>` shows the matching facts and asks which one to
delete. It never deletes on a fuzzy match the user has not seen.

Chat only. Agents still read memory and cannot write it, and the command
does not change that.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)


@command(
    name="memory",
    summary="See, add or forget what I remember about you",
    args=[
        Arg("text", kind="text", required=False,
            hint="A fact to remember, or 'forget <text>' to remove one."),
    ],
    kind="action",
    group="memory",
)
async def memory_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    from django.contrib.auth import get_user_model

    text = str(call.args.get("text") or call.text or "").strip()
    user = await get_user_model().objects.filter(id=ctx.user_id).afirst()
    if user is None:
        return CommandResult(status="error", message="No signed-in user.")
    if not text:
        # `/memory` alone: the client opens the panel from the card, backed
        # by the existing `UserMemoryView` (list by category, edit, delete).
        memories = await _list_memories(user)
        return CommandResult(
            status="ok",
            card={"type": "memory_list", "memories": memories},
        )
    lowered = text.lower()
    if lowered == "forget" or lowered.startswith("forget "):
        needle = text[len("forget"):].strip()
        if not needle:
            memories = await _list_memories(user)
            return CommandResult(
                status="ok",
                card={"type": "memory_forget_pick", "memories": memories},
            )
        matches = await _find_memories(user, needle)
        if not matches:
            return CommandResult(
                status="ok",
                message=f"Nothing stored matches '{needle}'.",
                card={"type": "memory_forget_pick", "memories": [], "query": needle},
            )
        # Never delete on a fuzzy match the user has not seen: show the
        # matches and ask which one to delete.
        return CommandResult(
            status="ok",
            card={"type": "memory_forget_pick", "memories": matches, "query": needle},
        )
    # `/memory <fact>`: write through `core/memory.py`, the same door the
    # `remember_about_user` tool uses.
    row, created = await sync_to_async(
        lambda: __import__("core.memory", fromlist=["remember"]).remember(
            user, text, source="user")
    )()
    if row is None:
        return CommandResult(status="error", message="Give the fact to remember.")
    return CommandResult(
        status="ok",
        message="Remembered." if created else "Already known — kept as is.",
        args={"text": row.text, "category": row.category},
        card={
            "type": "memory_saved", "text": row.text,
            "category": row.category, "created": created,
        },
    )


async def memory_delete(*, user, memory_id: int) -> bool:
    """Delete one fact by id, scoped to the owner (the 404-oracle rule)."""
    from core.models import UserMemory

    def _delete():
        deleted, _ = UserMemory.objects.filter(
            user=user, id=memory_id).delete()
        return deleted > 0

    return await sync_to_async(_delete)()


@sync_to_async
def _list_memories(user) -> list[dict]:
    from core.models import UserMemory

    rows = list(
        UserMemory.objects.filter(user=user)
        .order_by("category", "-updated_at")
        .values("id", "text", "category", "source")[:200]
    )
    return rows


@sync_to_async
def _find_memories(user, needle: str) -> list[dict]:
    from core.models import UserMemory

    rows = list(
        UserMemory.objects.filter(user=user, text__icontains=needle)
        .order_by("-updated_at")
        .values("id", "text", "category", "source")[:20]
    )
    return rows
