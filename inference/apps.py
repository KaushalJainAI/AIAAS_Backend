from django.apps import AppConfig


class InferenceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inference'

    def ready(self):
        """Trigger background initialization of the knowledge base."""
        import sys
        import os
        import threading
        
        # Avoid running during management commands like migrate or makemigrations
        if any(cmd in sys.argv for cmd in ['migrate', 'makemigrations', 'collectstatic', 'test']):
            return

        # Avoid running in the reloader main process; only run in the child worker
        # Alternatively, if we want it early, we can allow it, but for DB warnings,
        # we want to be careful.
        if os.environ.get('RUN_MAIN') != 'true' and os.environ.get('DJANGO_SETTINGS_MODULE'):
            # In development (with reloader), RUN_MAIN is not set in the parent process.
            # We skip parent to avoid duplicate loading of large models.
            return

        from .engine import get_platform_knowledge_base
        
        def warm_up():
            # This will run in a separate thread on startup
            import asyncio
            # Create a small worker loop just for this initialization if needed,
            # or simply call the async method via run() if not in a loop.
            # get_platform_knowledge_base().initialize() is async.
            try:
                loop = asyncio.new_event_loop()
                kb = get_platform_knowledge_base()
                loop.run_until_complete(kb.initialize())
                loop.close()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Background warm-up failed: {e}")

        threading.Thread(target=warm_up, daemon=True).start()
