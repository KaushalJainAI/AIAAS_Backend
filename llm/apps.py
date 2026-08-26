from django.apps import AppConfig


class LlmConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'llm'
    verbose_name = 'LLM Providers & Models'

    def ready(self):
        """
        Warm the provider registry at startup.

        Registration is lazy inside `get_registry()`; calling it here means
        the four supported providers are registered before the first request,
        so a misconfigured handler fails import time rather than mid-turn.
        """
        from .handlers.registry import get_registry

        get_registry()