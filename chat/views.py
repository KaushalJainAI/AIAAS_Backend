"""
HTTP surface for standalone chat.

These views do transport only: authenticate, parse, delegate, serialise. The
conversation logic lives in `pipeline` / `agent`, so the streaming and
non-streaming endpoints cannot drift apart — they are the same call with a
different `EventSink`. The plumbing they sit on is split out too:
`streaming_http` owns request and response mechanics, `attachments` owns the
file and RAG lifecycle.
"""
from __future__ import annotations

import json
import logging
from uuid import UUID

from adrf.decorators import api_view
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status, viewsets
from rest_framework.decorators import (
    api_view as sync_api_view,
    parser_classes,
    permission_classes,
)
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from workflow_backend.thresholds import (
    DOCUMENT_EXTRACT_CAP,
    IS_LARGE_FILE_THRESHOLD,
    LARGE_FILE_PREVIEW_LENGTH,
)

from chat.sources import attachments
from chat.turn import runs
from .turn.events import Event
from .models import ChatAttachment, ChatMessage, ChatSession
from .turn.pipeline import (
    TurnError,
    TurnRequest,
    persist_interrupted_answer,
    run_chat_turn,
)
from .serializers import ChatAttachmentSerializer, ChatSessionSerializer
from .transport.streaming_http import (
    authenticate,
    empty_stream,
    get_session,
    json_body,
    serialize_message,
    stream_response,
    unauthenticated,
)

logger = logging.getLogger(__name__)


# ── Sessions ─────────────────────────────────────────────────────────────────

class ChatSessionViewSet(viewsets.ModelViewSet):
    """CRUD for standalone chat sessions."""

    serializer_class = ChatSessionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ChatSession.objects.filter(user=self.request.user)

    def perform_create(self, serializer) -> None:
        serializer.save(user=self.request.user)

    def perform_destroy(self, instance) -> None:
        """Delete the session along with its RAG documents and vector index."""
        attachments.purge_session(instance)
        instance.delete()


# ── Sending messages ─────────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
async def send_message(request, session_id: str):
    """
    Send a message and wait for the complete reply.

    Same pipeline as the streaming endpoint with events discarded. Prefer
    `send_message_stream` in the UI — this one holds the connection open for the
    whole turn, tool calls included.
    """
    try:
        session = await get_session(session_id, request.user)
        turn = TurnRequest.parse(request.data)
        outcome = await run_chat_turn(session=session, user=request.user, request=turn)
    except TurnError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        "user_message": await serialize_message(outcome.user_message),
        "ai_response": await serialize_message(outcome.assistant_message),
    })


@csrf_exempt
async def send_message_stream(request, session_id: str):
    """
    Send a message and stream the turn as server-sent events.

    Emits `status`, `thinking_chunk`, `content_chunk`, `agent_trace`,
    `sources_update`, `images_update`, `videos_update`, `html_artifact`,
    `ask_permission` and a final `done`; `error` on failure.

    The response is only a view of the run: the turn is owned by `runs` and
    keeps going if this connection drops. Re-attach with `attach_message_stream`
    after a reload; `stop_message_stream` is the only thing that ends it early.
    """
    user = await authenticate(request)
    if user is None:
        return unauthenticated()

    # A send while this session is already answering would interleave two
    # transcripts. Attach to the turn in flight instead of starting a rival.
    live = runs.get(session_id)
    if live is not None and live.status == "running" and live.user_id == user.id:
        return stream_response(live)

    payload = json_body(request)

    async def work(sink) -> None:
        try:
            session = await get_session(session_id, user)
            outcome = await run_chat_turn(
                session=session,
                user=user,
                request=TurnRequest.parse(payload),
                sink=sink,
            )
        except TurnError as exc:
            await sink(Event.ERROR, {"message": str(exc)})
            return

        await sink(Event.DONE, {
            "user_message": await serialize_message(outcome.user_message),
            "ai_response": await serialize_message(outcome.assistant_message),
        })

    return stream_response(runs.start(session_id, user.id, work))


@csrf_exempt
async def attach_message_stream(request, session_id: str):
    """
    Re-attach to a turn already running for this session.

    How a reload recovers: the transcript is fetched as usual, then this
    replays every frame the turn has emitted and follows it live, so the
    half-written answer paints in exactly as if the page had never left. A
    `from` in the body skips frames the caller already has.

    Closing the stream with no frames means there is no run to attach to — the
    answer is in the database and the transcript already carries it.
    """
    user = await authenticate(request)
    if user is None:
        return unauthenticated()

    run = runs.get(session_id)
    if run is None or run.user_id != user.id:
        return empty_stream()

    from_index = json_body(request).get("from")
    return stream_response(run, from_index if isinstance(from_index, int) else 0)


@csrf_exempt
async def stop_message_stream(request, session_id: str):
    """
    Stop the turn running for this session — the one thing that cancels work.

    Whatever was streamed before the stop is persisted as the answer, flagged
    `interrupted`, so the transcript keeps the partial reply rather than losing
    the turn. Everyone still attached gets the closing `done` frame.
    """
    user = await authenticate(request)
    if user is None:
        return JsonResponse({"detail": "Authentication required."}, status=401)

    run = await runs.stop(session_id, user.id)
    if run is None:
        return JsonResponse({"detail": "No turn is running for this chat."}, status=404)

    message = None
    try:
        session = await get_session(session_id, user)
        message = await persist_interrupted_answer(
            session, user, runs.partial_answer(run)
        )
    except TurnError:
        logger.warning("[Stop] Session %s vanished mid-turn", session_id)

    serialized = await serialize_message(message) if message is not None else None
    await run.emit(Event.DONE, ai_response=serialized, stopped=True)
    runs.finish(run, "stopped")

    return JsonResponse({"stopped": True, "ai_response": serialized})


@csrf_exempt
async def steer_message_stream(request, session_id: str):
    """
    Say something to a turn that is already running.

    Not a second turn: `runs.start` would attach to the live one anyway, and
    two turns on one session interleave their frames into a single transcript.
    The message goes in the mailbox and the running graph picks it up at its
    next tool boundary — same run, same log, one continuous stream.

    404 when nothing is running, because "your message was delivered" and "your
    message went nowhere" must not look the same to the client.
    """
    from .turn import steering

    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)

    user = await authenticate(request)
    if user is None:
        return JsonResponse({"detail": "Authentication required."}, status=401)

    run = runs.get(session_id)
    if run is None or run.status != "running" or run.user_id != user.id:
        return JsonResponse(
            {"detail": "No turn is running for this chat."}, status=404,
        )

    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, TypeError):
        body = {}

    message = str(body.get("message") or "").strip()
    if not message:
        return JsonResponse({"detail": "A message is required."}, status=400)

    if not steering.post(session_id, message):
        return JsonResponse({"detail": "A message is required."}, status=400)

    return JsonResponse({"steered": True, **steering.stats(session_id)})


@sync_api_view(["GET"])
@permission_classes([IsAuthenticated])
def active_runs(request):
    """
    Session ids with a turn still running, so a freshly loaded page knows which
    conversations to re-attach to and which to mark as still working.
    """
    return Response({"active": runs.active_keys(request.user.id)})


# ── Deleting messages ────────────────────────────────────────────────────────

@sync_api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_message(request, session_id: str, message_id: int):
    """
    Delete a message, or rewind the conversation from it.

    `?rewind=true` removes this message and everything after it;
    `?rewind_after=true` keeps this message and removes what follows.
    """
    try:
        session = ChatSession.objects.filter(id=UUID(session_id), user=request.user).first()
    except (ValueError, TypeError):
        return Response({"error": "Invalid session id."}, status=400)

    if session is None:
        return Response({"error": "Chat session not found."}, status=404)

    message = get_object_or_404(ChatMessage, id=message_id, session=session)

    def flag(name: str) -> bool:
        return request.query_params.get(name, "").lower() == "true"

    if flag("rewind_after"):
        targets = ChatMessage.objects.filter(session=session, id__gt=message_id)
        detail = "Conversation rewound."
    elif flag("rewind"):
        targets = ChatMessage.objects.filter(session=session, id__gte=message_id)
        detail = "Conversation rewound."
    else:
        targets = ChatMessage.objects.filter(id=message.id)
        detail = "Message deleted."

    # Ids are sequential, so `id >=` is both cheaper and more precise than a
    # timestamp comparison when two messages share a creation second.
    for target in targets:
        attachments.release_attachment(session, target)
    targets.delete()

    return Response({"status": detail}, status=status.HTTP_204_NO_CONTENT)


# ── File upload ──────────────────────────────────────────────────────────────

@sync_api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_file(request, session_id: str):
    """Attach a file to a session, extracting its text for context and RAG."""
    try:
        session = ChatSession.objects.filter(id=UUID(session_id), user=request.user).first()
    except (ValueError, TypeError):
        return Response({"error": "Invalid session id."}, status=400)

    if session is None:
        return Response({"error": "Chat session not found."}, status=404)

    upload = request.FILES.get("file")
    if not upload:
        return Response({"error": "No file provided."}, status=400)

    file_type = attachments.classify_file(upload.name)
    data = upload.read()
    upload.seek(0)
    text = attachments.extract_text(data, file_type)

    attachment = ChatAttachment.objects.create(
        session=session,
        file=upload,
        filename=upload.name,
        file_type=file_type,
        file_size=upload.size,
        extracted_text=text[:DOCUMENT_EXTRACT_CAP],
        is_large_file=len(text) > IS_LARGE_FILE_THRESHOLD,
    )

    if file_type in ("pdf", "pptx", "text") and text:
        try:
            attachments.index_for_rag(request.user, upload, attachment, text)
        except Exception:
            logger.exception("[Upload] RAG indexing failed for %s", upload.name)

    preview = (
        f"\n\nContext summary ({len(text)} chars):\n{text[:LARGE_FILE_PREVIEW_LENGTH]}..."
        if text else ""
    )
    ChatMessage.objects.create(
        session=session,
        role="system",
        content=f"📎 File uploaded: **{upload.name}** ({file_type}, {upload.size} bytes){preview}",
        message_type="system",
        metadata={
            "attachment_id": str(attachment.id),
            "file_type": file_type,
            "has_extracted_text": bool(text),
        },
    )

    return Response({
        "attachment": ChatAttachmentSerializer(attachment).data,
        "extracted_text_length": len(text),
        "message": f'File "{upload.name}" uploaded successfully.',
    })


# ── Direct tool execution ────────────────────────────────────────────────────

@api_view(["POST"])
@permission_classes([IsAuthenticated])
async def execute_tool_view(request):
    """
    Run a single tool directly, for UI affordances that bypass the agent.

    Sensitive tools are refused here: they are gated by human approval inside
    the agent loop, and reaching them through this endpoint would be a way
    around that gate rather than a shortcut to it.

    "Sensitive" means the same thing here as it does in the loop, which is why
    this asks `chat.permissions` rather than only checking the name list. The
    list holds built-ins; a credentialed MCP write is not on it and was reachable
    through this endpoint with no gate at all — the loop would have paused for
    exactly the call this endpoint ran.
    """
    from chat.tools import permissions
    from .tools import SENSITIVE_TOOLS, execute_tool

    name = request.data.get("tool")
    args = request.data.get("args")

    if not name:
        return Response({"error": "'tool' is required."}, status=400)
    if not isinstance(args, dict):
        return Response({"error": "'args' must be an object."}, status=400)

    context = {"user_id": request.user.id}
    if name in SENSITIVE_TOOLS or await permissions.default_policy(name, args, context):
        return Response(
            {
                "error": "This tool requires human approval and cannot be run directly.",
                "status": "blocked",
            },
            status=403,
        )

    try:
        raw = await execute_tool(name, args, context)
    except Exception as exc:
        logger.exception("[Tools] Direct execution of %s failed", name)
        return Response({"error": str(exc)}, status=500)

    try:
        return Response(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return Response({"result": raw})
