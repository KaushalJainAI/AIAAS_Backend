"""
Deployment (server) settings — Redis channels, security headers on.

Config comes from the container environment, not a file: `.dockerignore` excludes
`.env*` (bar the examples), so nothing named `.env.deployment` is ever in the
image and the load_dotenv below is a no-op in production. The real values are
injected by `env_file: .env` in docker-compose.ec2.yml. The loader is kept only
so you can drop a `.env.deployment` beside manage.py to run these settings
locally — if you do, remember the server will not see it.

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

# ── CSRF trusted origins (required by Django 4+ for HTTPS POST requests) ─────
# Read from the environment, because this used to be a single hardcoded origin:
# deploying the same image behind any other hostname then failed CSRF on every
# session-authenticated POST (admin login, the allauth OAuth callback) with no
# clue in the response beyond "CSRF verification failed". Falling back to the
# hosts we already trust keeps a deploy that sets only ALLOWED_HOSTS working.
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
] or [
    f'https://{h}' for h in ALLOWED_HOSTS  # noqa: F405
    if h and h not in ('localhost', '127.0.0.1', 'backend', '*')
]
