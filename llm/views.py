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

from . import fallback as _fallback
from .catalog_refresh import REFRESH_STATUS_KEY
from django.core.cache import cache
from .effort import clean_levels, normalize as normalize_effort
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
                # the model is free and a platform key can actually pay for it.
                # The old rule (`provider_slug != 'ollama'`) treated every free
                # cloud model as runnable without a key — so a free Zen model
                # (no platform key by design, ToS) showed as available and then
                # failed at preflight: offered but unrunnable. A platform key
                # existing is what makes a free model runnable without the
                # user's own credential; Ollama needs nothing either way
                # because its provider is already available as keyless.
                payload = {
                    'name': m.name,
                    'value': m.value,
                    'is_free': m.is_free,
                    'description': m.description,
                    'available': provider_available or (
                        m.is_free and platform_api_key(provider_slug) is not None
                    ),
                    'input_price_per_million': str(m.input_price_per_million),
                    'output_price_per_million': str(m.output_price_per_million),
                    'cached_input_price_per_million': str(m.cached_input_price_per_million) if m.cached_input_price_per_million is not None else None,
                    'context_window': m.context_window,
                    # Cleaned rather than passed through: the column is
                    # admin-editable, and a picker rendering a rung the
                    # runtime would refuse to send is worse than one rendering
                    # none. `[]` is a real answer — this model has no effort
                    # control — which is why the key is always present.
                    'effort_levels': list(clean_levels(m.effort_levels)),
                    'default_effort': normalize_effort(m.default_effort) or '',
                    'supports_effort': bool(clean_levels(m.effort_levels)),
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

        # `meta` rides alongside, never inside, `providers`: old bundles read
        # the key they know and ignore the rest, so backend-only deploys keep
        # working. `last_refresh` is None until the first refresh runs.
        fallback_provider, fallback_model = _fallback.get_fallback()
        try:
            last_refresh = cache.get(REFRESH_STATUS_KEY)
        except Exception:  # noqa: BLE001 — meta must not fail the picker
            last_refresh = None
        return Response({
            'providers': data,
            'meta': {
                'last_refresh': last_refresh,
                'fallback': {
                    'provider': fallback_provider,
                    'model': fallback_model,
                },
            },
        })


class ModelRefreshView(APIView):
    """Re-diff the live OpenRouter catalogue against the held rows.

    Staff-only: the catalogue is global, and a refresh writes rows every
    account reads. Same service host cron drives
    (`manage.py refresh_models`); see `llm/catalog_refresh.py`.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from rest_framework.permissions import IsAdminUser

        if not IsAdminUser().has_permission(request, self):
            return Response(
                {'detail': 'Refreshing the model catalogue is staff-only.'},
                status=403,
            )
        from .catalog_refresh import (
            RefreshError,
            RefreshInProgress,
            refresh_catalog,
        )

        try:
            summary = refresh_catalog(user=request.user)
        except RefreshInProgress:
            return Response(
                {'detail': 'A catalogue refresh is already running.',
                 'code': 'refresh_in_progress'},
                status=409,
            )
        except RefreshError as exc:
            return Response(
                {'detail': str(exc), 'code': 'refresh_refused'},
                status=400,
            )
        summary = dict(summary)
        summary.pop('retired_values', None)
        return Response(summary)


class ModelFallbackView(APIView):
    """Read (everyone) and change (staff) the platform fallback model."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider, model = _fallback.get_fallback()
        return Response({'provider': provider, 'model': model})

    def patch(self, request):
        if not request.user.is_staff:
            return Response(
                {'detail': 'Changing the fallback model is staff-only.'},
                status=403,
            )
        from .models import AIModel
        from .providers import is_supported

        provider = (request.data.get('provider') or '').strip() or 'openrouter'
        model = (request.data.get('model') or '').strip()
        if not model:
            return Response(
                {'detail': 'model is required.'}, status=400)
        if not is_supported(provider):
            return Response(
                {'detail': f'Unknown provider "{provider}".'}, status=400)
        # A typo against a held catalogue is refused; an empty catalogue
        # (fresh install) is not — the same rule `AgentSerializer` applies,
        # or the fallback could never be set before the first seed.
        held = AIModel.objects.filter(provider__slug=provider)
        if held.exists() and not held.filter(value=model).exists():
            elsewhere = (AIModel.objects.filter(value=model)
                         .values_list('provider__slug', flat=True).first())
            hint = (f' "{model}" is a {elsewhere} model, not a {provider} one.'
                    if elsewhere else '')
            return Response(
                {'detail': f'"{model}" is not a {provider} model we hold.{hint}'},
                status=400,
            )
        row, warning = _fallback.set_fallback(
            provider, model, updated_by=request.user)
        payload = {'provider': row.provider, 'model': row.model}
        if warning:
            payload['warning'] = warning
        return Response(payload)
