"""
Deployment (server) settings.
Loads Backend/.env.deployment — PostgreSQL, Redis channels, security headers on.

Usage:
    DJANGO_SETTINGS_MODULE=workflow_backend.settings.deployment python manage.py runserver
    # Or set in docker-compose / systemd environment.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
load_dotenv(_BASE_DIR / '.env.deployment', override=True)

# Enforce production-safe defaults before base reads from env
os.environ.setdefault('DEBUG', 'False')
os.environ.setdefault('USE_REDIS_CHANNEL_LAYER', 'True')
os.environ.setdefault('CORS_ALLOW_ALL_ORIGINS', 'False')
os.environ.setdefault('GOOGLE_OAUTH_REDIRECT_URI', 'https://aiaas.kaushaljain.com/auth/google/callback')

from .base import *  # noqa: F401, F403, E402

# ── Security headers (only meaningful behind HTTPS) ──────────────────────────
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
