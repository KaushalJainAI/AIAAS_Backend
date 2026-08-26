import logging

from django.db.models import OuterRef, Subquery

from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Generation, ImagineConversation, ImagineMessage
from .serializers import (
    GenerationSerializer,
    ImagineConversationDetailSerializer,
    ImagineConversationSerializer,
)
from .services import catalog
from .services.capabilities import capabilities_for
from .services.dispatcher import run_generation
from .services.openrouter import MissingOpenRouterCredentialError, OpenRouterService

logger = logging.getLogger(__name__)


class ImagineGenerateThrottle(ScopedRateThrottle):
    """Per-user cap on endpoints that spend real money.

    The global `UserRateThrottle` (1000/hour) protects the API as a whole; it is
    not a cost guard. This is the one that is — a generation is a billed call,
    so it is limited the way `execute` is, not the way a list read is.
    """
    scope = 'imagine_generate'


class ImagineViewSet(viewsets.ModelViewSet):
    """Form-based generation (Advanced Mode)."""
    queryset = Generation.objects.all().order_by('-created_at')
    serializer_class = GenerationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    def get_throttles(self):
        throttles = super().get_throttles()
        if self.action == 'create':
            throttles = [*throttles, ImagineGenerateThrottle()]
        return throttles

    @action(detail=False, methods=['get'])
    def capabilities(self, request):
        """Model catalog per modality, plus the default selection for each.

        `?refresh=1` bypasses the hour-long cache — the picker offers this so a
        user who sees a stale list after OpenRouter ships a model does not have
        to wait it out.
        """
        # Probe the credential first so a missing one is reported as such
        # rather than as an empty catalog the UI cannot explain.
        try:
            OpenRouterService.for_user(request.user)
        except MissingOpenRouterCredentialError as e:
            return Response(
                {"detail": str(e), **catalog.EMPTY_CAPABILITIES, "defaults": {}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        refresh = request.query_params.get('refresh') in ('1', 'true', 'yes')
        caps = capabilities_for(request.user, refresh=refresh)
        return Response({
            **caps,
            "defaults": {
                kind: catalog.default_model_id(kind, caps.get(kind) or [])
                for kind in ("image", "video", "audio")
            },
            "recommended": catalog.RECOMMENDED,
        })

    def perform_create(self, serializer):
        # Preflight the credential so a user with no key gets a 400 naming the
        # problem rather than a 201 with a silently-failed row — the same
        # reasoning as the capabilities endpoint, and the same "fail before you
        # look busy" doctrine the chat pipeline preflights with.
        try:
            OpenRouterService.for_user(self.request.user)
        except MissingOpenRouterCredentialError as e:
            raise serializers.ValidationError({"detail": str(e)})
        generation = serializer.save(user=self.request.user)
        try:
            run_generation(generation)
        except Exception as e:
            logger.error(f"Failed to dispatch generation: {e}")
            generation.status = 'failed'
            generation.error_message = str(e)
            generation.save()


class ImagineAgentChatView(APIView):
    """Conversational entrypoint: NL message -> intent -> (HITL?) -> generation."""
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [*super().get_throttles(), ImagineGenerateThrottle()]

    def post(self, request):
        from .agent.graph import run_turn

        message = (request.data.get('message') or '').strip()
        if not message:
            return Response({"error": "message is required"}, status=status.HTTP_400_BAD_REQUEST)
        conversation_id = request.data.get('conversation_id')

        if conversation_id:
            try:
                conversation = ImagineConversation.objects.get(id=conversation_id, user=request.user)
            except ImagineConversation.DoesNotExist:
                return Response({"error": "conversation not found"}, status=status.HTTP_404_NOT_FOUND)
        else:
            conversation = ImagineConversation.objects.create(
                user=request.user,
                title=message[:60],
            )

        result = run_turn(
            conversation=conversation,
            user_message=message,
            preferred_model=(request.data.get('model') or None),
        )
        return Response(result)


class ImagineAgentResumeView(APIView):
    """Resume after HITL approval/edit/cancel."""
    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [*super().get_throttles(), ImagineGenerateThrottle()]

    def post(self, request):
        from .agent.graph import resume_turn

        conversation_id = request.data.get('conversation_id')
        decision = request.data.get('decision')
        overrides = request.data.get('overrides') or {}

        if decision not in ('approve', 'edit', 'cancel'):
            return Response({"error": "decision must be approve|edit|cancel"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            conversation = ImagineConversation.objects.get(id=conversation_id, user=request.user)
        except ImagineConversation.DoesNotExist:
            return Response({"error": "conversation not found"}, status=status.HTTP_404_NOT_FOUND)

        result = resume_turn(conversation=conversation, decision=decision, overrides=overrides)
        return Response(result)


class ImagineConversationViewSet(viewsets.ReadOnlyModelViewSet):
    """List/retrieve agent conversations with embedded messages."""
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        # `last_message` on the list serializer reads these annotations; the
        # alternative — one query per conversation for the last message — is
        # N+1 on every page of the list.
        last = ImagineMessage.objects.filter(conversation=OuterRef('pk')).order_by('-created_at')
        return (
            ImagineConversation.objects
            .filter(user=self.request.user)
            .order_by('-updated_at')
            .annotate(
                last_message_content=Subquery(last.values('content')[:1]),
                last_message_role=Subquery(last.values('role')[:1]),
            )
        )

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ImagineConversationDetailSerializer
        return ImagineConversationSerializer
