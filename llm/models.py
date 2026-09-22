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
    #: Separate from the read rate because the two differ by more than a
    #: factor of ten in opposite directions: OpenAI writes the cache for free
    #: and reads at 0.1x, while Anthropic charges 1.25x to write and 0.1x to
    #: read. Folding writes into the input rate understates an Anthropic bill
    #: on exactly the turns that write most — the long ones.
    #: NULL means "this provider does not charge to write", not "unknown".
    cache_write_price_per_million = models.DecimalField(
        max_digits=10, decimal_places=4, null=True, blank=True,
        help_text="USD per 1M cache-write tokens (blank = writes are free)",
    )
    context_window = models.IntegerField(
        default=0, help_text="Max context tokens (0 = unknown/variable)",
    )

    #: Where the row came from. `curated` rows are written by
    #: `populate_models.py`; `live` rows were discovered by a catalogue refresh
    #: (`llm/catalog_refresh.py`) against a provider's live /models endpoint;
    #: `hand` rows were added by a person in Django admin. The distinction is
    #: load-bearing: the seed's prune step and the live refresh only ever touch
    #: `curated` and `live` rows, so a hand-added row survives both.
    SOURCE_CHOICES = [
        ('curated', 'Curated seed'),
        ('live', 'Live catalogue refresh'),
        ('hand', 'Hand-added'),
    ]
    source = models.CharField(
        max_length=12, choices=SOURCE_CHOICES, default='curated',
        help_text='Which writer owns this row',
    )
    #: Last time a live refresh saw this id upstream. NULL means never seen
    #: live (a curated row predating refresh, or a hand-added one).
    last_seen_at = models.DateTimeField(null=True, blank=True)
    #: When the row was retired. Set rather than deleted: a saved run or agent
    #: may still reference the id, and a disabled row explains itself better
    #: than a missing one.
    retired_at = models.DateTimeField(null=True, blank=True)
    #: Suggested successor when retired, as a model value (e.g.
    #: `qwen/qwen3.8-max` → `qwen/qwen3.8-max-0902`). A hint for staff and for
    #: the refresh report — never applied automatically.
    replaced_by = models.CharField(
        max_length=150, blank=True, default='',
        help_text='Model value to suggest when this one is retired',
    )

    #: Which effort rungs this model actually offers, from `llm.effort.LADDER`.
    #: An empty list is a claim, not an absence: it says this model has no
    #: reasoning-effort control, which is why `llm.effort.resolve` returns None
    #: for it and no `reasoning_effort` field reaches the wire. Sending one to
    #: a model that does not take it is a hard 400 on OpenAI, so "we don't
    #: know" and "it has none" have to look the same downstream.
    effort_levels = models.JSONField(
        default=list, blank=True,
        help_text=(
            "Reasoning-effort levels this model offers, e.g. "
            '["low", "medium", "high"]. Empty = no effort control.'
        ),
    )
    #: The rung to use when the caller names none. Blank means "let the
    #: provider decide", which is the honest answer for a model whose own
    #: default we have not measured — and it is distinct from picking `medium`
    #: on its behalf, which would silently change what an unconfigured call
    #: costs.
    default_effort = models.CharField(
        max_length=10, blank=True, default="",
        help_text="Effort level used when the caller names none (blank = provider default)",
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


class ModelFallback(models.Model):
    """The platform-wide fallback model.

    One row (pk=1), edited by staff through `/api/llm/fallback/` or Django
    admin — deliberately a row and not an env var, so changing it needs
    neither an image rebuild nor a container restart. Read through
    `llm/fallback.py::get_fallback` (60s TTL cache), never directly, so an
    edit takes effect without a restart.

    Used when an agent or chat session names a model that is retired or
    unknown: the run executes on the fallback and the owner is notified, while
    the stored configuration is left untouched.
    """

    provider = models.CharField(max_length=30, default='openrouter')
    model = models.CharField(max_length=150, default='openrouter/free')
    updated_by = models.ForeignKey(
        'auth.User', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='model_fallback_edits',
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Model fallback'
        verbose_name_plural = 'Model fallback'

    def __str__(self):
        return f"{self.provider}/{self.model}"


class ModelFallbackNotice(models.Model):
    """One notified (agent, retired model) pair.

    A scheduled agent would otherwise ping its owner on every run after its
    model died. The unique pair is the whole rate limit: the first
    substitution (or refresh) writes the row and notifies; later runs find it
    and stay quiet.
    """

    subagent = models.ForeignKey(
        'orchestrator.SubAgent', on_delete=models.CASCADE,
        related_name='fallback_notices',
    )
    old_value = models.CharField(
        max_length=150,
        help_text='The retired llm_model value the owner was told about',
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['subagent', 'old_value']

    def __str__(self):
        return f'{self.subagent_id}: {self.old_value}'
