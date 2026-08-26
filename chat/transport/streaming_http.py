"""
Transport plumbing for the chat HTTP surface.

Nothing here knows what a turn is. These are the pieces the views need to get
bytes on and off the wire: request parsing, bearer-token auth for views DRF's
decorators cannot wrap, ORM serialisation from async code, and rendering a
`runs.ChatRun` as `text/event-stream`.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from asgiref.sync import sync_to_async
from django.http import StreamingHttpResponse

from chat.turn import runs
from chat.models import ChatSession
from chat.turn.pipeline import TurnError
from chat.serializers import ChatAttachmentSerializer, ChatMessageSerializer
from .sse import error_frame, frame

logger = logging.getLogger(__name__)


serialize_message = sync_to_async(lambda m: ChatMessageSerializer(m).data)
serialize_attachment = sync_to_async(lambda a: ChatAttachmentSerializer(a).data)


async def get_session(session_id: str, user) -> ChatSession:
    """Fetch the caller's session, or raise `TurnError` with a safe message."""
    try:
        session_uuid = UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise TurnError("Invalid session id.") from None

    session = await ChatSession.objects.filter(id=session_uuid, user=user).afirst()
    if session is None:
        raise TurnError("Chat session not found.")
    return session


def json_body(request) -> dict[str, Any]:
    """Parse a plain-Django request body. Streaming views have no `request.data`."""
    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return body if isinstance(body, dict) else {}


async def authenticate(request):
    """
    Resolve the user for a view DRF's decorators cannot wrap.

    `StreamingHttpResponse` bypasses DRF's authentication, so the bearer token
    is verified here instead.
    """
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return user

    header = request.META.get("HTTP_AUTHORIZATION", "")
    if not header.startswith("Bearer "):
        return None

    from django.contrib.auth import get_user_model
    from rest_framework_simplejwt.exceptions import TokenError
    from rest_framework_simplejwt.tokens import AccessToken

    try:
        token = AccessToken(header.removeprefix("Bearer ").strip())
        return await get_user_model().objects.aget(id=token["user_id"])
    except (TokenError, KeyError, get_user_model().DoesNotExist) as exc:
        logger.info("[Auth] Rejected streaming request: %s", exc)
        return None


def stream_response(run: runs.ChatRun, from_index: int = 0) -> StreamingHttpResponse:
    """Render a run as `text/event-stream`, replaying from `from_index`."""

    async def frames():
        async for event, payload in runs.subscribe(run, from_index):
            yield frame(event, payload)

    response = StreamingHttpResponse(frames(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"  # let nginx pass chunks straight through
    return response


def empty_stream() -> StreamingHttpResponse:
    """A stream that ends immediately: nothing is running to attach to."""
    response = StreamingHttpResponse(iter(()), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def unauthenticated() -> StreamingHttpResponse:
    return StreamingHttpResponse(
        iter([error_frame("Authentication required.")]),
        content_type="text/event-stream",
    )
