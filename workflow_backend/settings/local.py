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
