"""
Guest (unauthenticated) chat endpoints.

Keeps the heavy authenticated chat pipeline untouched. Guests get:
  - One owner: the shared guest Django user (see chat/guest.py).
  - One provider: NVIDIA NIM, with the key from settings.NVIDIA_API_KEY.
  - Plain chat only — no file uploads, workflow suggestions, KB / RAG, MCP,
    or canvas-agent routing. Web search and code execution are out of scope
    for the guest agentic loop; visitors must log in to use those.
  - IP-based rate limits via the three GuestChat* throttle classes.
  - A 200K token (input + output) budget enforced before the upstream call.
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.http import StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import (
    api_view as sync_api_view,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from core.throttling import (
    GuestChatDayThrottle,
    GuestChatHourThrottle,
    GuestChatMinuteThrottle,
)

from .guest import (
    fits_token_budget,
    get_guest_user,
    stream_nvidia_chat,
)
from .models import ChatMessage, ChatSession
from .serializers import ChatMessageSerializer, ChatSessionSerializer

logger = logging.getLogger(__name__)

GUEST_THROTTLES = [GuestChatMinuteThrottle, GuestChatHourThrottle, GuestChatDayThrottle]
GUEST_SYSTEM_PROMPT = (
    "You are AIAAS Guest Chat, a helpful AI assistant powered by NVIDIA NIM. "
    "Answer concisely. Use Markdown when helpful. "
    "If the user asks about features that require a login (file uploads, workflows, "
    "knowledge base, integrations, canvas help-agent), explain that they need to log "
    "in to access those, but still answer plain questions directly."
)
HISTORY_TURNS_FOR_GUEST = 10  # last N messages included in the context


@sync_api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes(GUEST_THROTTLES)
def create_guest_session(request):
    """Create a new ChatSession owned by the shared guest user."""
    from .guest import get_guest_user_sync

    title = (request.data.get("title") or "New Chat").strip()[:255] or "New Chat"
    guest = get_guest_user_sync()

    session = ChatSession.objects.create(
        user=guest,
        title=title,
        llm_provider="nvidia",
        llm_model=settings.NVIDIA_GUEST_MODEL,
        intent="chat",
        system_prompt=GUEST_SYSTEM_PROMPT,
    )
    return Response(ChatSessionSerializer(session).data, status=status.HTTP_201_CREATED)


@sync_api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes(GUEST_THROTTLES)
def get_guest_session(request, session_id):
    """Fetch a guest-owned session by UUID. 404 for sessions owned by real users."""
    from .guest import get_guest_user_sync

    try:
        session_uuid = UUID(str(session_id))
    except (TypeError, ValueError):
        return Response({"detail": "Invalid session id"}, status=status.HTTP_400_BAD_REQUEST)

    guest = get_guest_user_sync()
    session = ChatSession.objects.filter(id=session_uuid, user=guest).first()
    if not session:
        return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(ChatSessionSerializer(session).data)


@csrf_exempt
async def guest_send_message_stream(request, session_id: str):
    """
    SSE streaming endpoint for guest chat.
    Emits events compatible with the existing StandaloneChat frontend:
        status, content_chunk, thinking_chunk, done, error
    """
    # Manual throttling — DRF decorators don't bind cleanly to bare async views.
    if request.method != "POST":
        return _error_response("Method not allowed", http_status=405)

    throttle_failure = await sync_to_async(_check_guest_throttles)(request)
    if throttle_failure is not None:
        return throttle_failure

    try:
        req_data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _error_response("Invalid JSON body", http_status=400)

    content = (req_data.get("content") or "").strip()
    if not content:
        return _error_response("Content is required", http_status=400)

    guest = await get_guest_user()

    try:
        session_uuid = UUID(str(session_id))
    except (TypeError, ValueError):
        return _error_response("Invalid session id", http_status=400)

    session = await ChatSession.objects.filter(id=session_uuid, user=guest).afirst()
    if not session:
        return _error_response("Chat session not found", http_status=404)

    async def event_stream():
        try:
            # Build conversation history (oldest -> newest, last N).
            history_msgs = []
            async for msg in ChatMessage.objects.filter(session=session).order_by("-created_at")[:HISTORY_TURNS_FOR_GUEST]:
                history_msgs.append(msg)
            history_msgs.reverse()

            system_prompt = session.system_prompt or GUEST_SYSTEM_PROMPT
            messages = [{"role": "system", "content": system_prompt}]
            for m in history_msgs:
                if m.role in ("user", "assistant"):
                    messages.append({"role": m.role, "content": m.content})
            messages.append({"role": "user", "content": content})

            # Token-budget guard.
            combined_text = "\n".join(m["content"] for m in messages)
            max_output_tokens = 4096
            if not fits_token_budget(combined_text, max_output_tokens):
                yield _sse({"type": "error", "message": "Conversation exceeds the 200K token guest limit. Log in to use larger contexts."})
                return

            # Persist the user message.
            user_msg = await ChatMessage.objects.acreate(
                session=session, role="user", content=content, message_type="chat",
            )
            yield _sse({
                "type": "status", "phase": "thinking",
                "message": "Talking to NVIDIA NIM...",
                "user_message_id": user_msg.id,
            })

            # Stream completion.
            accumulated = []
            thinking_accum = []
            had_error = False
            async for chunk in stream_nvidia_chat(
                messages,
                model=session.llm_model or settings.NVIDIA_GUEST_MODEL,
                max_tokens=max_output_tokens,
            ):
                ctype = chunk.get("type")
                if ctype == "content":
                    text = chunk.get("content") or ""
                    if text:
                        accumulated.append(text)
                        yield _sse({"type": "content_chunk", "content": text})
                elif ctype == "thinking":
                    text = chunk.get("content") or ""
                    if text:
                        thinking_accum.append(text)
                        yield _sse({"type": "thinking_chunk", "content": text})
                elif ctype == "error":
                    had_error = True
                    yield _sse({"type": "error", "message": chunk.get("message") or "NVIDIA error"})
                    break
                elif ctype == "done":
                    break

            final_text = "".join(accumulated).strip() or ("[no response]" if had_error else "")
            if had_error and not final_text:
                return

            ai_msg = await ChatMessage.objects.acreate(
                session=session,
                role="assistant",
                content=final_text,
                message_type="chat",
                metadata={"provider": "nvidia", "model": session.llm_model, "guest": True},
            )

            user_payload = await sync_to_async(lambda: ChatMessageSerializer(user_msg).data)()
            ai_payload = await sync_to_async(lambda: ChatMessageSerializer(ai_msg).data)()

            yield _sse({
                "type": "done",
                "user_message": user_payload,
                "ai_response": ai_payload,
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("Guest stream failure")
            yield _sse({"type": "error", "message": f"Server error: {exc}"})

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ----- helpers -----

def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _error_response(message: str, http_status: int):
    """Return an SSE-shaped error so the frontend's stream parser stays happy."""
    body = _sse({"type": "error", "message": message})
    resp = StreamingHttpResponse(iter([body]), content_type="text/event-stream", status=http_status)
    resp["Cache-Control"] = "no-cache"
    return resp


def _check_guest_throttles(request):
    """Run each guest throttle synchronously; return an SSE error response on hit."""
    for ThrottleCls in GUEST_THROTTLES:
        throttle = ThrottleCls()
        if not throttle.allow_request(request, None):
            wait = throttle.wait() or 60
            return _error_response(
                f"Rate limit exceeded. Try again in {int(wait)}s, or log in for higher limits.",
                http_status=429,
            )
    return None
