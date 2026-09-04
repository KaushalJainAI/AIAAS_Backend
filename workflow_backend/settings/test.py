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
# The platform keys. Chat preflights the credential before it will start a
# turn, so without one every pipeline test fails on 'no verified credential'
# rather than on what it asserts. Neither value is ever sent anywhere: tests
# patch the provider call itself.
#
# OpenRouter joined NVIDIA here on 2026-09-03, when the default provider moved
# — and the 13 pipeline tests that broke in between are the useful part of the
# story. They were not asserting anything about NVIDIA; they were relying on
# the *shipped default* being runnable, which is precisely the property a
# platform key provides. So the rule this file encodes is: whatever
# `ChatSession.llm_provider` defaults to must have a key here, because a
# default nobody can run is not a default.
os.environ.setdefault('NVIDIA_API_KEY', 'test-platform-key-not-a-real-key')
os.environ.setdefault('OPENROUTER_API_KEY', 'test-platform-key-not-a-real-key')

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

REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    'DEFAULT_THROTTLE_CLASSES': [],
    'DEFAULT_THROTTLE_RATES': {
        k: '100000/second' for k in REST_FRAMEWORK.get('DEFAULT_THROTTLE_RATES', {})
    },
}

# In-process checkpoints. The graph is compiled once at import, so a file-backed
# saver would mean every test run sharing one checkpoint database — and tests
# reuse thread ids freely, so state would leak between cases in a way that
# depends on execution order. Durability is what `chat/tests/test_checkpoints.py`
# and `logs/tests/test_checkpoints.py` assert *about*, not something they need.
AGENT_CHECKPOINTER = 'memory'
