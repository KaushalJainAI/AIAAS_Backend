from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from .models import Generation
from .serializers import GenerationSerializer
from .services.openrouter import OpenRouterService
from .tasks import poll_video_generation
from django.core.cache import cache
import logging

logger = logging.getLogger(__name__)

class ImagineViewSet(viewsets.ModelViewSet):
    queryset = Generation.objects.all().order_by('-created_at')
    serializer_class = GenerationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    @action(detail=False, methods=['get'])
    def capabilities(self, request):
        """
        Returns dynamic capabilities for different models and types from OpenRouter.
        """
        # Cache capabilities for 1 hour to avoid repeated API calls
        capabilities = cache.get("openrouter_capabilities")
        if not capabilities:
            capabilities = OpenRouterService.fetch_models()
            # Only cache if we actually got some data
            if capabilities.get("image") or capabilities.get("video") or capabilities.get("audio"):
                cache.set("openrouter_capabilities", capabilities, 3600)
        
        return Response(capabilities)

    def perform_create(self, serializer):
        # The user will select a model ID which is stored in the 'model' field
        generation = serializer.save(user=self.request.user)
        
        # Trigger real generation via OpenRouter
        try:
            self.dispatch_generation(generation)
        except Exception as e:
            logger.error(f"Failed to dispatch generation: {e}")
            generation.status = 'failed'
            generation.error_message = str(e)
            generation.save()

    def dispatch_generation(self, instance):
        config = {
            "aspect_ratio": instance.aspect_ratio,
            "image_size": self._map_resolution_to_size(instance.resolution),
            "resolution": instance.resolution,
            "duration": instance.duration,
            "negative_prompt": instance.negative_prompt,
            "seed": instance.seed,
            "voice": instance.voice,
            "speed": instance.speed,
        }

        if instance.type == 'image':
            result = OpenRouterService.generate_image(instance.prompt, instance.model, config)
            if "error" in result:
                instance.status = 'failed'
                instance.error_message = result["error"]
            else:
                instance.status = 'completed'
                instance.output_url = result["url"]
            instance.save()

        elif instance.type == 'video':
            result = OpenRouterService.generate_video(instance.prompt, instance.model, config)
            if "error" in result:
                instance.status = 'failed'
                instance.error_message = result["error"]
                instance.save()
            else:
                instance.status = 'pending'
                instance.job_id = result["job_id"]
                instance.polling_url = result["polling_url"]
                instance.save()
                # Trigger polling task
                poll_video_generation.delay(instance.id)

        elif instance.type == 'audio':
            result = OpenRouterService.generate_audio(instance.prompt, instance.model, config)
            if "error" in result:
                instance.status = 'failed'
                instance.error_message = result["error"]
            else:
                instance.status = 'completed'
                instance.output_url = result["url"]
            instance.save()

    def _map_resolution_to_size(self, res_str):
        if not res_str:
            return "1K"
        if "1024" in res_str:
            return "1K"
        if "2048" in res_str or "2K" in res_str.upper():
            return "2K"
        if "4096" in res_str or "4K" in res_str.upper():
            return "4K"
        return "1K"
