"""
NVIDIA NIM adapter for the `nemotron-parse` cross-check.

The witness itself goes through `chat.llm.complete`, which already knows how to
resolve credentials and speak to NIM. The parser cannot: it has two quirks that
no general chat path handles.

1. It refuses text input — "The model does not support text input" — so the
   content array must be image-only, with no text part in front of it.
2. It answers with a **tool call**, not message content: `message.content` is
   null and `tool_calls[0].function.arguments` carries a `markdown_bbox` array of
   `{bbox, text, type}` regions.

Hence a small direct client rather than another branch inside the shared
handler, where both quirks would read as bugs.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging

import httpx

logger = logging.getLogger(__name__)

NIM_CHAT_URL = "https://integrate.api.nvidia.com/v1/chat/completions"

PARSE_TIMEOUT_SECONDS = 20.0
PARSE_ATTEMPTS = 2
#: Kept short: the parser is a *second opinion*. If it is slow or throttled the
#: right answer is to return the witness's reading unqualified, not to hold up
#: the main agent's turn waiting for a confirmation.
PARSE_BACKOFF_SECONDS = 0.75

#: How much transcript to hand back. The parser returns every region it found,
#: which on a dense page is a wall of text the main agent does not need — the
#: disagreement signal lives in the first few readings.
MAX_PARSE_CHARS = 1_200


def encode_attachment(attachment) -> tuple[str, str] | None:
    """
    `(base64_data, mime_type)` for an attachment, or None if it cannot be read.

    Path validation is the shared one from the workflow encoder rather than a
    second implementation: the witness reads files by path on behalf of a model
    that was handed an id, which is exactly the shape traversal likes.
    """
    from llm.handlers.openai_compatible import validate_attachment_path

    try:
        path = attachment.file.path if hasattr(attachment.file, "path") else attachment.file.name
    except (AttributeError, ValueError):
        return None

    if not path or not validate_attachment_path(path):
        logger.warning("[Vision] Blocked or missing attachment path: %s",
                       getattr(attachment, "filename", "?"))
        return None

    mime = mime_for(getattr(attachment, "filename", "") or path)
    try:
        with open(path, "rb") as fh:
            return base64.b64encode(fh.read()).decode("utf-8"), mime
    except OSError as exc:
        logger.warning("[Vision] Cannot read attachment %s: %s",
                       getattr(attachment, "filename", "?"), exc)
        return None


def mime_for(filename: str) -> str:
    """Image mime type from a filename. Shared with the workflow encoder.

    Imported rather than reimplemented: the witness and the main chat path must
    label the same file identically, or a bug in one is invisible in the other.
    """
    from llm.handlers.openai_compatible import image_mime_for

    return image_mime_for(filename)


async def parse_image(attachment, *, api_key: str, model: str) -> str | None:
    """
    Transcribe an image with `nemotron-parse`, or None if it could not be run.

    None means "no second opinion available" and must leave the witness's answer
    untouched. A failed cross-check is not evidence of anything.
    """
    encoded = encode_attachment(attachment)
    if encoded is None:
        return None
    data, mime = encoded

    payload = {
        "model": model,
        # Image-only: a text part here is rejected outright by this model.
        "messages": [{
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            }],
        }],
        "temperature": 0.0,
        "max_tokens": 2048,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    for attempt in range(PARSE_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=PARSE_TIMEOUT_SECONDS) as client:
                response = await client.post(NIM_CHAT_URL, json=payload, headers=headers)
            if response.status_code == 404:
                # Not entitled for this account. Retrying will not change that.
                logger.info("[Vision] Parser %s not available for this account", model)
                return None
            if response.status_code >= 400:
                # Log the body: a probe run hit sustained 400s that did not
                # reproduce, and the body is the only thing that would have said
                # why. Without it the next person guesses again.
                logger.warning("[Vision] Parser HTTP %s: %s",
                               response.status_code, response.text[:500])
                if attempt + 1 < PARSE_ATTEMPTS:
                    await asyncio.sleep(PARSE_BACKOFF_SECONDS * (attempt + 1))
                    continue
                return None
            return extract_parse_text(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("[Vision] Parser call failed: %s", exc)
            if attempt + 1 < PARSE_ATTEMPTS:
                await asyncio.sleep(PARSE_BACKOFF_SECONDS * (attempt + 1))

    return None


def extract_parse_text(body: dict) -> str | None:
    """Pull the readable transcript out of a `nemotron-parse` response body."""
    try:
        message = (body.get("choices") or [{}])[0].get("message") or {}
    except (AttributeError, IndexError, TypeError):
        return None

    # Content first, on the chance a future revision starts filling it in.
    if content := (message.get("content") or "").strip():
        return content[:MAX_PARSE_CHARS]

    for call in message.get("tool_calls") or []:
        raw = ((call or {}).get("function") or {}).get("arguments")
        if not raw:
            continue
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            continue
        regions = args.get("markdown_bbox") if isinstance(args, dict) else None
        if not isinstance(regions, list):
            continue
        texts = [
            str(r.get("text", "")).strip()
            for r in regions
            if isinstance(r, dict) and str(r.get("text", "")).strip()
        ]
        if texts:
            return " | ".join(texts)[:MAX_PARSE_CHARS]

    return None
