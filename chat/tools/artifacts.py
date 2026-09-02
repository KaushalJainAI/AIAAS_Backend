"""
The HTML artifact tool: renders into a sandboxed iframe with no network, no
same-origin access and no session. Not the code sandbox — different mechanism,
different threat model, so it lives in its own module.
"""
from __future__ import annotations

import json
import logging

from typing import Dict

from workflow_backend.thresholds import (
    HTML_ARTIFACT_MAX_CHARS,
    HTML_ARTIFACT_MAX_WIDTH,
    HTML_ARTIFACT_MAX_HEIGHT,
    HTML_ARTIFACT_MIN_WIDTH,
    HTML_ARTIFACT_MIN_HEIGHT,
    HTML_ARTIFACT_DEFAULT_WIDTH,
    HTML_ARTIFACT_DEFAULT_HEIGHT,
)

from .registry import tool

logger = logging.getLogger(__name__)


@tool({
        "type": "function",
        "function": {
            "name": "render_html_artifact",
            "description": "Render a self-contained HTML/CSS/JS snippet as a live, interactive card in the chat. Use for charts, diagrams, tables, small demos or anything better shown than described. The snippet runs in a locked-down sandbox: it has no network access, no access to the page around it, and no access to the user's session. Inline all CSS and JS — external files will not load.",
            "parameters": {
                "type": "object",
                "properties": {
                    "html": {
                        "type": "string",
                        "description": "A complete, self-contained HTML document or fragment."
                    },
                    "title": {
                        "type": "string",
                        "description": "Short label shown on the card header."
                    },
                    "width": {
                        "type": "integer",
                        "description": "Requested width in px. Clamped to 160-720."
                    },
                    "height": {
                        "type": "integer",
                        "description": "Requested height in px. Clamped to 120-520."
                    }
                },
                "required": [
                    "html"
                ],
                "additionalProperties": False
            }
        }
    }, effect="read")
async def render_html_artifact(args: Dict, context: Dict) -> str:
    """
    Hand a self-contained HTML snippet to the client for sandboxed rendering.

    Nothing is executed here. The server's job is to bound the payload and
    pin the dimensions; the frontend renders it in a sandboxed iframe.

    Clamping server-side matters even though the frontend clamps too: the
    stored metadata is replayed when the conversation is reloaded, so an
    unclamped size persisted today becomes an oversized card on every future
    render. Bound it once, at write time.
    """
    html = args.get("html") or ""
    if not html.strip():
        return json.dumps({"error": "Missing html."})

    truncated = False
    if len(html) > HTML_ARTIFACT_MAX_CHARS:
        html = html[:HTML_ARTIFACT_MAX_CHARS]
        truncated = True

    def _clamp(raw, default, lo, hi) -> int:
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    width = _clamp(args.get("width"), HTML_ARTIFACT_DEFAULT_WIDTH,
                   HTML_ARTIFACT_MIN_WIDTH, HTML_ARTIFACT_MAX_WIDTH)
    height = _clamp(args.get("height"), HTML_ARTIFACT_DEFAULT_HEIGHT,
                    HTML_ARTIFACT_MIN_HEIGHT, HTML_ARTIFACT_MAX_HEIGHT)

    title = (args.get("title") or "Rendered output").strip()[:80]

    return json.dumps({
        "type": "html_artifact",
        "title": title,
        "html": html,
        "width": width,
        "height": height,
        "truncated": truncated,
        "note": (
            f"Rendered at {width}x{height}px in a sandboxed frame."
            + (" Content was truncated to fit the size limit." if truncated else "")
        ),
    })
