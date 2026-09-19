"""
Publish a hosted page: a snapshot of an output shareable by link.

A page is a **snapshot, not a pointer** — the body is frozen at publish time,
so editing the source afterwards never changes what somebody already holds a
link to. Withdrawing unlists rather than deletes.

Outward-facing, so `sensitive=True` (chat asks before publishing) and
`effect="irreversible"` (the autonomy ladder gates it with no new mechanism:
`ask` pauses, `auto` pauses, `plan` withholds it, `full` does not). Never in
`ALWAYS_AVAILABLE`, and never above `link` visibility from an unattended run
without a human — a schedule publishing to the open internet while nobody is
watching is refused, not queued, because the decision is already recorded
nowhere and there is no second copy to send.
"""
from __future__ import annotations

import json
import logging

from typing import Dict

from .registry import tool

logger = logging.getLogger(__name__)

#: Callers where no human is present at the moment the run starts. Mirrors
#: `agents/agent/runtime.py::UNATTENDED_CALLERS` without importing it — this
#: module is imported by the tool registry at URLconf time, and the agent
#: runtime is deliberately lazily imported.
UNATTENDED_CALLERS = frozenset({'trigger', 'orchestrator'})


@tool({
    "type": "function",
    "function": {
        "name": "publish_page",
        "description": (
            "Publish a hosted page: a snapshot of a report, an HTML document, "
            "or a file, shareable by link. Use it when the user asks to share "
            "or publish something — a report they can send, a page they can "
            "link. `report` takes markdown — put a chart in a fenced ```chart "
            "block holding the same JSON render_chart takes, and the page draws "
            "it; `html` takes a full self-contained HTML document with "
            "inline CSS/JS and no external requests; `file` takes "
            '{"document_id": N} and snapshots that file. '
            "Visibility is link (anyone with the link, still needs an account), "
            "platform (listed to signed-in users), or public (readable with no "
            "account at /p/<slug>). Default platform; pick the narrowest that "
            "does the job — sharing with one colleague is not publishing to "
            "the open internet. The snapshot never changes after publishing; "
            "publish again for a new version."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "The page title.",
                },
                "kind": {
                    "type": "string",
                    "enum": ["report", "html", "file"],
                    "description": "report (markdown), html, or file.",
                },
                "body": {
                    "type": "string",
                    "description": "The frozen content: markdown, HTML, or JSON like {\"document_id\": 12}.",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["link", "platform", "public"],
                    "description": "How wide the link reaches. Default platform.",
                },
            },
            "required": ["title", "kind", "body"],
            "additionalProperties": False,
        },
    },
}, sensitive=True, effect="irreversible")
async def publish_page(args: Dict, context: Dict) -> str:
    """Freeze a snapshot and hand back its link."""
    from asgiref.sync import sync_to_async
    from django.contrib.auth import get_user_model

    from inference.pages import PublishError, publish

    visibility = (args.get("visibility") or "platform").strip()
    if visibility != "link" and context.get("caller") in UNATTENDED_CALLERS:
        return json.dumps({
            "error": "Publishing above link visibility from an unattended run "
                     "needs a human watching. Publish as link, or run this "
                     "attended."
        })

    user = await get_user_model().objects.filter(id=context.get("user_id")).afirst()
    if user is None:
        return json.dumps({"error": "Publishing needs a signed-in user."})
    try:
        page = await sync_to_async(publish)(
            user, title=args.get("title") or "", kind=args.get("kind") or "report",
            body=args.get("body") or "", visibility=visibility,
        )
    except PublishError as exc:
        return json.dumps({"error": str(exc)})
    except Exception:
        logger.exception("[Publish] page publish failed")
        return json.dumps({"error": "The page could not be published."})

    link = f"/p/{page.slug}"
    return json.dumps({
        "slug": page.slug,
        "url": link,
        "title": page.title,
        "visibility": page.visibility,
        "rendered": (
            f"Published {link} ({page.visibility}). Give the user the link as "
            f"[{page.title}]({link}) — do not paste the page's contents back."
        ),
    })
