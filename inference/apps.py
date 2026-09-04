import logging
import os
import sys
import threading
from django.apps import AppConfig

logger = logging.getLogger(__name__)

#: Entry points that must not pay for (or be perturbed by) an embedder load.
#: `pytest` is in here for the same reason `test` is — the pytest runner never
#: puts the word "test" in argv, so the preload thread started on every test
#: run and reached for NVIDIA_API_KEY during collection.
_SKIP_CMDS = {
    'migrate', 'makemigrations', 'collectstatic', 'test', 'shell', 'dbshell',
    'pytest', 'py.test',
}


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
        # Signals are registered unconditionally — unlike the embedder preload
        # below, `doc_count` must stay true under `manage.py` commands and in
        # tests too, since those delete documents as much as the API does.
        from . import signals  # noqa: F401

        argv = {os.path.basename(a) for a in sys.argv}
        if argv & _SKIP_CMDS:
            return
        if os.environ.get('PRELOAD_EMBEDDER', 'True').lower() not in ('true', '1', 'yes'):
            return
        threading.Thread(target=_safe_preload, daemon=True, name='embedder-preload').start()
