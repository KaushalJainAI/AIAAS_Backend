"""
`/code-review [target]` — a read-only review that answers with findings, not prose (§18.5).

Targets, resolved by the argument completer: a `CodeProject`'s uncommitted
diff (P6 `git_diff`), a GitHub PR URL (read through the vault token), or VFS
files/folders (`/code-review /Agents/Reporter/`). Runs the `reviewer`
gallery template under **`plan` autonomy** (a review never edits), with
`shell` narrowed by `toolScope` to its read tools, plus read-only `fileOps`.
A new output contract `findings` in `agents/contracts.py`
(`[{file, line, severity, category, summary, suggestion}]`) is rendered as a
findings card with a per-item "Fix it" that starts a normal turn scoped to
that finding. Fixing stays a separate step that can be approved.

Hidden when there is neither a workspace engine nor any code file in the VFS.
"""
from __future__ import annotations

import logging

from asgiref.sync import sync_to_async

from .registry import Arg, CommandCall, CommandContext, CommandResult, command

logger = logging.getLogger(__name__)

REVIEWER_SLUG = "reviewer"


@command(
    name="code-review",
    summary="Review a project diff, PR or folder — findings, not prose",
    args=[
        Arg("target", kind="text", required=False,
            hint="A project name, PR URL, or /Agents/... path. Omit to pick."),
    ],
    kind="turn",
    requires="workspace",
    group="code",
)
async def code_review_command(call: CommandCall, ctx: CommandContext) -> CommandResult:
    from workspaces.engine import workspace_available

    if not workspace_available():
        return CommandResult(
            status="error",
            message="Code review needs a workspace engine, and none is configured.",
        )
    target = str(call.args.get("target") or call.text or "").strip()
    projects = await _project_names(ctx)
    if not target:
        if not projects:
            return CommandResult(
                status="error",
                message="No code projects yet. Connect one first, then ask for a review.",
            )
        return CommandResult(
            status="confirm",
            message="Which code should be reviewed?",
            args={},
            card={"type": "code_review_pick", "projects": projects},
        )
    resolved = await _resolve_target(target, ctx, projects)
    if resolved is None:
        return CommandResult(
            status="error",
            message=f"Nothing reviewable called '{target}'. Pick a project, a PR URL or a file path.",
        )
    kind = resolved["kind"]
    if kind == "pr" and not str(target).strip().lower().startswith("http"):
        return CommandResult(status="error", message="That PR URL does not look like one.")
    context_block = (
        f"[COMMAND /code-review {resolved['label']}]\n"
        f"Review {resolved['label']} read-only and answer with findings, not prose. "
        f"Use the reviewer instructions: severities (blocker, major, minor, nit), "
        f"categories (correctness, security, performance, readability, tests), "
        f"one finding per issue with file, line, summary and a concrete suggestion. "
        f"Never edit anything — fixing is a separate approved step. "
        f"Target details: {resolved['detail']}"
    )
    return CommandResult(
        status="ok",
        args={"target": resolved["label"], "kind": kind},
        context_block=context_block,
        # Read tools only: shell reads + file reads. The turn runs under the
        # chat `plan`-equivalent — enforced by narrowing, not by gating.
        tool_pin=(
            "ws_list", "ws_read", "ws_search", "git_status", "git_diff",
            "list_files", "find_files", "read_file",
        ),
    )


@sync_to_async
def _project_names(ctx: CommandContext) -> list[str]:
    from workspaces.models import CodeProject

    return list(
        CodeProject.objects.filter(user_id=ctx.user_id)
        .order_by("name")
        .values_list("name", flat=True)[:100]
    )


async def _resolve_target(
    target: str, ctx: CommandContext, projects: list[str]
) -> dict | None:
    text = (target or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return {"kind": "pr", "label": text,
                "detail": f"a GitHub PR at {text}, read through the vault token"}
    for name in projects:
        if name.lower() == lowered:
            return {"kind": "project", "label": name,
                    "detail": f"the uncommitted diff of code project '{name}' (git_diff) "
                              f"plus its working tree (ws_read)"}
    if text.startswith("/"):
        return {"kind": "files", "label": text,
                "detail": f"VFS files under {text} (list_files/read_file)"}
    # A bare word that is not a project may still be a VFS-relative path.
    return {"kind": "files", "label": text,
            "detail": f"VFS files at {text} (list_files/read_file)"}
