"""
What the model picker reads.

One endpoint, and the only place that answers "which models may this user pick
right now". Availability is computed against `credentials.resolution` — the same
lookup the executor performs when it actually runs the call — so a model the
picker offers is one that will execute rather than fail at request time.
"""
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from credentials.models import Credential
from credentials.resolution import KEYLESS_PROVIDERS, platform_api_key, slugs_for

from .models import AIProvider
from .providers import SUPPORTED_PROVIDERS

#: Capability flags copied verbatim into each model's payload. Listed once
#: because the frontend keys off every one of them; adding a flag to the model
#: without adding it here makes it invisible rather than false.
CAPABILITY_FIELDS = (
    'supports_text_input',
    'supports_text_generation',
    'supports_image_input',
    'supports_image_generation',
    'supports_audio_input',
    'supports_audio_generation',
    'supports_video_input',
    'supports_video_generation',
    'supports_numeric_input',
    'supports_numeric_generation',
    'supports_time_series_input',
    'supports_time_series_generation',
    'supports_document_input',
    'supports_document_generation',
    'supports_tabular_input',
    'supports_tabular_generation',
    'supports_structured_output',
    'supports_tool_calling',
    'supports_embedding_generation',
)


@method_decorator(never_cache, name='get')
class AIModelListView(APIView):
    """
    List all available AI providers and their models.
    Also returns whether the user has verified credentials for each provider
    and computes dynamic availability for providers and models.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # Filtered by the supported set rather than `is_active` alone: retired
        # providers can linger in the table on an instance whose seed script has
        # not been re-run, and offering one means offering a provider with no
        # handler behind it.
        providers = (
            AIProvider.objects
            .filter(is_active=True, slug__in=SUPPORTED_PROVIDERS)
            .prefetch_related('models')
        )

        # Get user's verified credentials
        verified_type_slugs = set(
            Credential.objects.filter(
                user=request.user,
                is_active=True,
                is_verified=True
            ).values_list('credential_type__slug', flat=True)
        )

        data = []
        for provider in providers:
            provider_slug = provider.slug

            # `slugs_for` is the same mapping the executor resolves credentials
            # with, so what the picker shows as available is what will actually
            # run. Keyless providers (Ollama) need nothing configured, and a
            # platform key makes a provider usable before the user has any
            # credential of their own.
            has_creds = (
                provider_slug in KEYLESS_PROVIDERS
                or bool(verified_type_slugs.intersection(slugs_for(provider_slug)))
                or platform_api_key(provider_slug) is not None
            )

            provider_available = has_creds

            model_data = []
            for m in provider.models.filter(is_active=True):
                # Model is available if its provider is fully available, or if
                # the model is free and the provider isn't local: free cloud
                # models route through the platform key, paid ones need the
                # user's own verified credential.
                payload = {
                    'name': m.name,
                    'value': m.value,
                    'is_free': m.is_free,
                    'description': m.description,
                    'available': provider_available or (m.is_free and provider_slug != 'ollama'),
                    'input_price_per_million': str(m.input_price_per_million),
                    'output_price_per_million': str(m.output_price_per_million),
                    'cached_input_price_per_million': str(m.cached_input_price_per_million) if m.cached_input_price_per_million is not None else None,
                    'context_window': m.context_window,
                    'pricing_usd_per_million': {
                        'input': str(m.input_price_per_million),
                        'output': str(m.output_price_per_million),
                        'cached_input': str(m.cached_input_price_per_million) if m.cached_input_price_per_million is not None else None,
                    },
                }
                payload.update({field: getattr(m, field) for field in CAPABILITY_FIELDS})
                model_data.append(payload)

            data.append({
                'name': provider.name,
                'slug': provider.slug,
                'description': provider.description,
                'icon': provider.icon,
                'has_credentials': has_creds,
                'available': provider_available,
                'models': model_data,
            })

        return Response({'providers': data})
