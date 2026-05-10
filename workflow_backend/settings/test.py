"""
Test settings — always uses in-process SQLite and in-memory cache.
No Redis, no remote DB, no external services needed.

Usage:
    python manage.py test --settings=workflow_backend.settings.test
"""
import os

# Force SQLite before base.py reads from env
os.environ['DATABASE_URL'] = ''
os.environ['DB_ENGINE'] = 'sqlite'
os.environ['SQLITE_PATH'] = ':memory:'
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')
os.environ.setdefault('CREDENTIAL_ENCRYPTION_KEY', 'SG8t-4Tj4BlST1p7VD5OhRVXMUjdxY9m2ZeadrSVCvU=')
os.environ.setdefault('DEBUG', 'True')
os.environ.setdefault('USE_REDIS_CHANNEL_LAYER', 'False')
os.environ.setdefault('RUN_WORKFLOWS_ASYNC', 'False')

from .base import *  # noqa: F401, F403

# In-memory SQLite — fastest possible test DB
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# Dummy cache — no Redis required
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

# Silence migration output during tests
class DisableMigrations:
    def __contains__(self, item):
        return True
    def __getitem__(self, item):
        return None

# Run all migrations normally so models are available
# (DisableMigrations skips them — only enable if tests are too slow)

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

CHANNEL_LAYERS = {
    'default': {
        'BACKEND': 'channels.layers.InMemoryChannelLayer',
    }
}
