import logging

from django.core.cache import cache
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Generation, ImagineConversation
from .serializers import (
    GenerationSerializer,
    ImagineConversationDetailSerializer,
    ImagineConversationSerializer,
)
from .services.dispatcher import run_generation
from .services.openrouter import MissingOpenRouterCredentialError, OpenRouterService

logger = logging.getLogger(__name__)


class ImagineViewSet(viewsets.ModelViewSet):
    """Form-based generation (Advanced Mode)."""
    queryset = Generation.objects.all().order_by('-created_at')
    serializer_class = GenerationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    @action(detail=False, methods=['get'])
    def capabilities(self, request):
        capabilities = cache.get("openrouter_capabilities")
        if capabilities:
            return Response(capabilities)
        try:
            service = OpenRouterService.for_user(request.user)
        except MissingOpenRouterCredentialError as e:
            return Response(
                {"detail": str(e), "image": [], "video": [], "audio": []},
                status=status.HTTP_400_BAD_REQUEST,
            )
        capabilities = service.fetch_models()
        if capabilities.get("image") or capabilities.get("video") or capabilities.get("audio"):
            cache.set("openrouter_capabilities", capabilities, 3600)
        return Response(capabilities)

    def perform_create(self, serializer):
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

        result = run_turn(conversation=conversation, user_message=message)
        return Response(result)


class ImagineAgentResumeView(APIView):
    """Resume after HITL approval/edit/cancel."""
    permission_classes = [IsAuthenticated]

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
        return ImagineConversation.objects.filter(user=self.request.user).order_by('-updated_at')

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ImagineConversationDetailSerializer
        return ImagineConversationSerializer
