import sys
import threading
from django.apps import AppConfig


_SKIP_CMDS = {'migrate', 'makemigrations', 'collectstatic', 'test', 'shell', 'dbshell'}


class InferenceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inference'

    def ready(self):
        # Skip during management commands that don't serve requests
        if set(sys.argv) & _SKIP_CMDS:
            return

        # ready() fires after ALL Django apps and their modules are fully imported,
        # so transformers / torch / bitsandbytes are safe to import here with no races.
        from .engine import _preload_embedder
        threading.Thread(target=_preload_embedder, daemon=True, name='embedder-preload').start()
