"""
The provider/model registry.

`providers.py` answers *which* provider slugs this platform supports — a
constant, four entries long, that never touches the database. These two models
answer the other half: which concrete models each provider currently offers,
what each one can accept and emit, and whether it is still active. That is data,
reseeded by `populate_models.py` against live provider catalogues, which is why
it lives in a table rather than beside the constant.

Both tables keep their original `nodes_*` names. They were born in the `nodes`
app and moved here when the AI layer was split out; renaming the tables would
have bought nothing but a migration that has to be sequenced against a running
deploy.
"""
from decimal import Decimal

from django.db import models


class AIProvider(models.Model):
    """
    AI model providers (e.g., OpenAI, Gemini, Ollama).
    """
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'nodes_aiprovider'
        verbose_name = 'AI Provider'
        verbose_name_plural = 'AI Providers'
        ordering = ['name']

    def __str__(self):
        return self.name


class AIModel(models.Model):
    """
    Specific AI models belonging to a provider.
    """
    provider = models.ForeignKey(AIProvider, on_delete=models.CASCADE, related_name='models')
    name = models.CharField(max_length=100)
    value = models.CharField(max_length=150, unique=True, help_text='The technical name/ID of the model')
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_free = models.BooleanField(default=False)

    # Capability Flags
    supports_text_input = models.BooleanField(default=True)
    supports_text_generation = models.BooleanField(default=True)
    supports_image_input = models.BooleanField(default=False)
    supports_image_generation = models.BooleanField(default=False)
    supports_audio_input = models.BooleanField(default=False)
    supports_audio_generation = models.BooleanField(default=False)
    supports_video_input = models.BooleanField(default=False)
    supports_video_generation = models.BooleanField(default=False)
    supports_numeric_input = models.BooleanField(default=False)
    supports_numeric_generation = models.BooleanField(default=False)
    supports_time_series_input = models.BooleanField(default=False)
    supports_time_series_generation = models.BooleanField(default=False)
    supports_document_input = models.BooleanField(default=False)
    supports_document_generation = models.BooleanField(default=False)
    supports_tabular_input = models.BooleanField(default=False)
    supports_tabular_generation = models.BooleanField(default=False)
    supports_structured_output = models.BooleanField(default=False)
    supports_tool_calling = models.BooleanField(default=False)
    supports_embedding_generation = models.BooleanField(default=False)

    # Pricing — per 1M tokens in USD, as listed by the provider / OpenRouter
    # on 2026-08-24. Used for billing estimates and spend-cap math.
    # Local (Ollama) models stay 0. Free cloud models are 0 on the meter but
    # still carry the upstream list price in `description` for reference.
    input_price_per_million = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("0.0000"),
        help_text="USD per 1M input tokens (0 = local/free)",
    )
    output_price_per_million = models.DecimalField(
        max_digits=10, decimal_places=4, default=Decimal("0.0000"),
        help_text="USD per 1M output tokens",
    )
    cached_input_price_per_million = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
        help_text="USD per 1M cached input tokens, if provider offers caching",
    )
    context_window = models.IntegerField(
        default=0, help_text="Max context tokens (0 = unknown/variable)",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'nodes_aimodel'
        verbose_name = 'AI Model'
        verbose_name_plural = 'AI Models'
        ordering = ['provider', 'name']

    def __str__(self):
        return f"{self.provider.name} - {self.name}"
