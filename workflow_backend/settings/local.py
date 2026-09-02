"""
Local development settings.
Loads Backend/.env.local — SQLite, in-memory channels, no Redis required.

Usage:
    DJANGO_SETTINGS_MODULE=workflow_backend.settings.local python manage.py runserver
"""

import os
from pathlib import Path
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(_BASE_DIR / '.env.local', override=True)

# The shared test configuration, layered on top: `instance/test.env` holds the
# settings a test run needs (database, hosts, email backend, the E2E rate lane)
# so they live in one tracked file the whole team shares, rather than in each
# developer's untracked .env.local.
#
# `override=False` is the load-bearing part: .env.local wins wherever both
# define a key, so a local tweak is never silently clobbered by the shared file.
# Secrets are deliberately not in it -- SECRET_KEY, CREDENTIAL_ENCRYPTION_KEY
# and the provider API keys stay in .env.local, which is untracked; test.env is
# tracked, which is exactly why a live key must never be pasted there.
#
# Local settings only. `deployment.py` does not load this, so nothing here can
# reach production.
load_dotenv(_BASE_DIR.parent / 'instance' / 'test.env', override=False)

# Set sensible local defaults before base reads from env
os.environ.setdefault('DEBUG', 'True')
if not os.environ.get('DATABASE_URL'):
    os.environ.setdefault('DB_ENGINE', 'sqlite')
os.environ.setdefault('USE_REDIS_CHANNEL_LAYER', 'False')
os.environ.setdefault('RUN_WORKFLOWS_ASYNC', 'False')
os.environ.setdefault('CORS_ALLOW_ALL_ORIGINS', 'True')
os.environ.setdefault('PUBLIC_URL', 'http://localhost:8000')
os.environ.setdefault('GOOGLE_OAUTH_REDIRECT_URI', 'http://localhost:3000/auth/google/callback')

from .base import *  # noqa: F401, F403, E402
