import logging
import os
import sys
import threading
from django.apps import AppConfig

logger = logging.getLogger(__name__)

_SKIP_CMDS = {'migrate', 'makemigrations', 'collectstatic', 'test', 'shell', 'dbshell'}


def _safe_preload():
    try:
        from .engine import _preload_embedder
        _preload_embedder()
    except BaseException as exc:  # incl. SystemExit / OOM-related aborts caught at Python layer
        logger.warning("Embedder preload failed (will load lazily on first use): %r", exc)


class InferenceConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'inference'

    def ready(self):
        if set(sys.argv) & _SKIP_CMDS:
            return
        if os.environ.get('PRELOAD_EMBEDDER', 'True').lower() not in ('true', '1', 'yes'):
            return
        threading.Thread(target=_safe_preload, daemon=True, name='embedder-preload').start()
