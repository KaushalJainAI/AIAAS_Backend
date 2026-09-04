"""
Base Django settings — shared across all environments.
Do NOT import this directly. Use settings.local or settings.deployment.
"""

import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Backend/ directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _split_env_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(',') if item.strip()]


def _database_config():
    database_url = os.environ.get('DATABASE_URL', '').strip()

    if database_url:
        parsed = urlparse(database_url)
        query_params = parse_qs(parsed.query)
        engine_map = {
            'postgres': 'django.db.backends.postgresql',
            'postgresql': 'django.db.backends.postgresql',
            'sqlite': 'django.db.backends.sqlite3',
        }
        engine = engine_map.get(parsed.scheme)

        if engine == 'django.db.backends.sqlite3':
            db_name = parsed.path or '/app/data/db.sqlite3'
            return {
                'ENGINE': engine,
                'NAME': db_name.lstrip('/'),
                'OPTIONS': {'timeout': 20},
            }

        if engine:
            options = {}
            sslmode = query_params.get('sslmode', [os.environ.get('DB_SSLMODE', '')])[0]
            if sslmode:
                options['sslmode'] = sslmode
            return {
                'ENGINE': engine,
                'NAME': parsed.path.lstrip('/'),
                'USER': parsed.username or '',
                'PASSWORD': parsed.password or '',
                'HOST': parsed.hostname or '',
                'PORT': str(parsed.port or ''),
                'CONN_MAX_AGE': int(os.environ.get('DB_CONN_MAX_AGE', '60')),
                'OPTIONS': options,
            }

    db_engine = os.environ.get('DB_ENGINE', 'sqlite').strip().lower()
    if db_engine in {'postgres', 'postgresql'}:
        options = {}
        sslmode = os.environ.get('DB_SSLMODE', '').strip()
        if sslmode:
            options['sslmode'] = sslmode
        return {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ.get('POSTGRES_DB', 'aiaas'),
            'USER': os.environ.get('POSTGRES_USER', 'postgres'),
            'PASSWORD': os.environ.get('POSTGRES_PASSWORD', ''),
            'HOST': os.environ.get('POSTGRES_HOST', 'localhost'),
            'PORT': os.environ.get('POSTGRES_PORT', '5432'),
            'CONN_MAX_AGE': int(os.environ.get('DB_CONN_MAX_AGE', '60')),
            'OPTIONS': options,
        }

    # Resolve a relative SQLITE_PATH against BASE_DIR, never against the current
    # working directory: manage.py runs from Backend/ while instance/scripts/ and
    # the harnesses run from the repo root, and a cwd-relative path silently gives
    # each of them a *different* database file.
    sqlite_path = Path(os.environ.get('SQLITE_PATH', 'db.sqlite3'))
    if not sqlite_path.is_absolute():
        sqlite_path = BASE_DIR / sqlite_path

    return {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': sqlite_path,
        'OPTIONS': {'timeout': 20},
    }


DEBUG = os.environ.get('DEBUG', 'False') == 'True'

#: Shared secret an automated E2E client presents (header `X-E2E-Bypass-Token`)
#: to be exempted from the *rate limits only* -- see
#: `core.http.throttling.is_test_client`. Blank disables the whole mechanism,
#: which is the default and what production should keep: the throttles are a
#: brute-force guard, and the adversarial harness asserts they still fire.
E2E_THROTTLE_BYPASS_TOKEN = os.environ.get('E2E_THROTTLE_BYPASS_TOKEN', '')

# A missing SECRET_KEY must never silently fall back to a shared, well-known
# value in production: that key signs JWTs and session cookies, so a known key
# lets anyone forge either. In DEBUG we tolerate an ephemeral dev key; outside
# DEBUG an unset key is a hard configuration error.
SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-dev-only-key-not-for-production'
    else:
        raise RuntimeError(
            'SECRET_KEY environment variable is required when DEBUG is False.'
        )

ALLOWED_HOSTS = [
    'localhost',
    '127.0.0.1',
    '.ngrok-free.app',
    '.ngrok-free.dev',
    os.environ.get('PUBLIC_URL', '').replace('https://', '').replace('http://', '').split('/')[0],
]
ALLOWED_HOSTS.extend(_split_env_list(os.environ.get('ALLOWED_HOSTS', '')))
ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h]

# Trust the same hostnames for CSRF (needed for the admin / cookie-auth POSTs
# over HTTPS; the JWT API auth is header-based and does not rely on this).
CSRF_TRUSTED_ORIGINS = [
    f'https://{h}' for h in ALLOWED_HOSTS
    if h and not h.startswith('.') and h not in ('localhost', '127.0.0.1')
]
CSRF_TRUSTED_ORIGINS += ['http://localhost:5173', 'http://localhost:3000', 'http://localhost:8000']

INSTALLED_APPS = [
    'daphne',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'drf_spectacular',
    'rest_framework_simplejwt',
    'corsheaders',
    'channels',
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'dj_rest_auth',
    'dj_rest_auth.registration',
    'core',
    'llm',
    'executor',
    'agents',
    'credentials',
    'inference',
    'logs',
    'streaming',
    'templates',
    'mcp_integration',
    'skills',
    'chat',
    'django_celery_beat',
    'notifications',
    'imagine',
    'tools_config',
    # Evaluation of sub-agents. Label is `eval` (singular): the deleted `evals`
    # app left inert `evals_*` tables and an `evals.0001_initial` row in dev
    # databases, so the new tables must not be named the same thing.
    'eval',
]

SITE_ID = 1

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    # Serves STATIC_ROOT itself. runserver only serves static when DEBUG is on,
    # so with DEBUG=False the admin loaded with no CSS; nginx proxies /static
    # straight through to Django, so there was nothing else to fall back on.
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'core.http.middleware.RequestLoggingMiddleware',
    'core.http.middleware.InputSanitizationMiddleware',
    'core.http.middleware.RateLimitHeaderMiddleware',
]

ROOT_URLCONF = 'workflow_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'workflow_backend.wsgi.application'

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

DATABASES = {
    'default': _database_config()
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATICFILES_DIRS = [d for d in [BASE_DIR / 'static'] if d.exists()]
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_ROOT = BASE_DIR / 'media' / 'documents'

FAISS_INDEX_DIR = BASE_DIR / 'media' / 'indices'
FAISS_INDEX_DIR.mkdir(parents=True, exist_ok=True)

# ── S3 (django-storages) — mirrors the NGU project's pattern ─────────────
USE_S3 = os.environ.get('USE_S3', 'False').lower() in ('true', '1', 'yes')

if USE_S3:
    # django-storages is only registered when S3 is actually enabled
    # (the package lives in requirements-linux.txt, not the Windows requirements).
    if 'storages' not in INSTALLED_APPS:
        INSTALLED_APPS.append('storages')

    AWS_ACCESS_KEY_ID = os.environ.get('AWS_ACCESS_KEY_ID', '')
    AWS_SECRET_ACCESS_KEY = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
    AWS_STORAGE_BUCKET_NAME = os.environ.get('AWS_STORAGE_BUCKET_NAME', 'aiaas-bucket-07')
    AWS_S3_REGION_NAME = os.environ.get('AWS_S3_REGION_NAME', 'ap-south-1')
    AWS_S3_SIGNATURE_VERSION = os.environ.get('AWS_S3_SIGNATURE_VERSION', 's3v4')

    AWS_S3_CUSTOM_DOMAIN = f'{AWS_STORAGE_BUCKET_NAME}.s3.{AWS_S3_REGION_NAME}.amazonaws.com'
    AWS_S3_OBJECT_PARAMETERS = {'CacheControl': 'max-age=86400'}
    AWS_DEFAULT_ACL = None            # Bucket policy controls access, not ACL
    AWS_S3_FILE_OVERWRITE = False
    AWS_QUERYSTRING_AUTH = False      # Public, unsigned URLs

    STATIC_LOCATION = os.environ.get('AWS_STATIC_LOCATION', 'aiaas/static')
    MEDIA_LOCATION = os.environ.get('AWS_MEDIA_LOCATION', 'aiaas/media')

    STORAGES = {
        'default': {
            'BACKEND': 'storages.backends.s3boto3.S3Boto3Storage',
            'OPTIONS': {'location': MEDIA_LOCATION},
        },
        'staticfiles': {
            'BACKEND': 'storages.backends.s3boto3.S3StaticStorage',
            'OPTIONS': {'location': STATIC_LOCATION},
        },
    }

    STATIC_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/{STATIC_LOCATION}/'
    MEDIA_URL = f'https://{AWS_S3_CUSTOM_DOMAIN}/{MEDIA_LOCATION}/'
else:
    STATIC_URL = '/static/'
    MEDIA_URL = '/media/'

# ---------------------------------------------------------------------------
# Code execution sandbox
# ---------------------------------------------------------------------------
# `service` runs the `execute_python` tool in the hardened sidecar container
# (sandbox_service/) — real confinement, numpy/pandas available. `inprocess` is
# the weaker AST-guarded dev fallback in sandbox/safe_execution.py. The deployed
# image sets SANDBOX_ENGINE=service; a bare local runserver defaults to
# inprocess so it works with no sidecar. There is no automatic fallback between
# them — see sandbox/engine.py.
SANDBOX_ENGINE = os.environ.get('SANDBOX_ENGINE', 'inprocess')
SANDBOX_SERVICE_URL = os.environ.get('SANDBOX_SERVICE_URL', 'http://sandbox:8100')
SANDBOX_WALL_SECONDS = int(os.environ.get('SANDBOX_WALL_SECONDS', '10'))
SANDBOX_CPU_SECONDS = int(os.environ.get('SANDBOX_CPU_SECONDS', '8'))
SANDBOX_MEM_MB = int(os.environ.get('SANDBOX_MEM_MB', '384'))

# ---------------------------------------------------------------------------
# Agent run checkpoints
# ---------------------------------------------------------------------------
# Where a run's graph state lives, and therefore whether a run survives the
# process that started it. `memory` is in-process and loses every in-flight run
# on restart; `sqlite` is a file beside the dev database; `postgres` is for a
# deployment with more than one process or a replaceable container. One door,
# no automatic fallback between them — see chat/turn/checkpoints.py.
#
# The dev default is `sqlite` rather than `memory` because the failure it
# prevents is invisible: an interrupted run leaves an ExecutionLog on `running`
# for ever and a user watching a stream attached to nothing.
AGENT_CHECKPOINTER = os.environ.get('AGENT_CHECKPOINTER', 'sqlite')
# Its own file, never db.sqlite3: checkpoint writes are heavy and would take
# SQLite's single write lock on the application database on every super-step.
AGENT_CHECKPOINT_PATH = os.environ.get('AGENT_CHECKPOINT_PATH', '')
AGENT_CHECKPOINT_DSN = os.environ.get('AGENT_CHECKPOINT_DSN', '')
# How often to look for runs whose process is gone.
RUN_RECOVERY_SWEEP_SECONDS = int(
    os.environ.get('RUN_RECOVERY_SWEEP_SECONDS', '600')
)

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'core.auth.query_param_jwt.QueryParamJWTAuthentication',
        'core.auth.authentication.APIKeyAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.IsAuthenticated',
    ),
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    'DEFAULT_THROTTLE_RATES': {
        'anon': '100/hour',
        'user': '1000/hour',
        'login': '5/minute',
        'register': '3/minute',
        'compile': '10/minute',
        'execute': '5/minute',
        'chat': '20/hour',
        'stream': '20/minute',
        'imagine_generate': '30/hour',
        'password_reset': '10/hour',
        'password_change': '10/hour',
        'guest_chat_min': '3/minute',
        'guest_chat_hour': '15/hour',
        'guest_chat_day': '50/day',
    },
}

# --- Guest chat (anonymous OpenRouter free-router demo) ---
# NVIDIA_API_KEY is still read here: it remains the platform default key for
# authenticated users with no credential of their own (PLATFORM_ENV_KEYS in
# credentials/resolution.py). Guest chat no longer uses it.
NVIDIA_API_KEY = os.environ.get('NVIDIA_API_KEY', '')
# Which model guests get is NOT configurable here: it is pinned to OpenRouter's
# free-models router in chat/guest/runtime.py (GUEST_PROVIDER/GUEST_MODEL), and
# its key comes from OPENROUTER_API_KEY via platform_api_key(). An env override
# of the *model* would let a deploy quietly serve anonymous visitors on a model
# nobody chose to pay for, which is the opposite of what the pin is for.
GUEST_CHAT_MAX_TOKENS = int(os.environ.get('GUEST_CHAT_MAX_TOKENS', '200000'))
GUEST_USER_EMAIL = os.environ.get('GUEST_USER_EMAIL', 'guest@aiaas.local')

SPECTACULAR_SETTINGS = {
    'TITLE': 'AIAAS API',
    'DESCRIPTION': 'AI as a Service — workflow orchestration, inference, credentials, and more.',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
    'COMPONENT_SPLIT_REQUEST': True,
    'SECURITY': [
        {'jwtAuth': []},
        {'apiKeyAuth': []},
    ],
    'COMPONENTS': {
        'securitySchemes': {
            'jwtAuth': {
                'type': 'http',
                'scheme': 'bearer',
                'bearerFormat': 'JWT',
            },
            'apiKeyAuth': {
                'type': 'apiKey',
                'in': 'header',
                'name': 'X-API-Key',
            },
        },
    },
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(minutes=360),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'AUTH_HEADER_TYPES': ('Bearer',),
}

# These look unused, but a settings module *is* its namespace — Django reads
# these names off it directly, so the import is the assignment. The module is
# present; the guard only covers a build that strips it.
try:
    from workflow_backend.thresholds import (  # noqa: F401
        DATA_UPLOAD_MAX_MEMORY_SIZE,
        FILE_UPLOAD_MAX_MEMORY_SIZE,
        DATA_UPLOAD_MAX_NUMBER_FIELDS,
    )
except ImportError:
    pass

CORS_ALLOW_ALL_ORIGINS = os.environ.get('CORS_ALLOW_ALL_ORIGINS', 'False') == 'True'
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOWED_ORIGINS = _split_env_list(os.environ.get('CORS_ALLOWED_ORIGINS', ''))

ASGI_APPLICATION = 'workflow_backend.asgi.application'

REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
USE_REDIS_CHANNEL_LAYER = os.environ.get('USE_REDIS_CHANNEL_LAYER', 'False') == 'True'

if USE_REDIS_CHANNEL_LAYER:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [REDIS_URL]},
        },
    }
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer',
        },
    }

# ── Cache ───────────────────────────────────────────────────────────────────
#
# There was no `CACHES` block here at all, which is not the same as there being
# no cache: Django falls back to `LocMemCache`, silently, per process. Every
# `django.core.cache` user was therefore holding a private copy that died with
# the worker — and the most expensive of them is `mcp_integration/tool_cache.py`,
# whose whole purpose is to keep a ~21-second cold `npx` off the front of a
# user's first token. A per-process cache means that cost is paid again by every
# worker, and again after every deploy.
#
# Two other things only work on a shared backend. `tool_cache.invalidate_user`
# needs `delete_pattern`, which LocMem does not implement — so editing a
# connection left stale tools advertised until the TTL lapsed (`imporvements.md`
# §3.3). And `llm/access.py`'s effort-support cache, primed by `preflight` and
# read synchronously on the hot path, was primed in one process and read in
# another.
#
# Opt-in by URL rather than by environment name: `USE_REDIS_CACHE` defaults on
# wherever the channel layer is already on Redis, because a deployment with a
# broker has one. Local dev with no Redis keeps LocMem and needs no config —
# the same rule the channel layer above follows, and for the same reason.
USE_REDIS_CACHE = os.environ.get(
    'USE_REDIS_CACHE', 'True' if USE_REDIS_CHANNEL_LAYER else 'False',
) == 'True'

if USE_REDIS_CACHE:
    CACHES = {
        'default': {
            # Django's own backend (5.x), so no `django-redis` dependency and
            # no second connection-pool implementation to reason about.
            'BACKEND': 'django.core.cache.backends.redis.RedisCache',
            # A different logical database from the channel layer and Celery:
            # a `flushdb` while debugging a queue must not also drop every
            # cached tool list and re-cold-start every connector.
            'LOCATION': os.environ.get('CACHE_URL', REDIS_URL.rsplit('/', 1)[0] + '/1'),
            'KEY_PREFIX': 'aiaas',
        }
    }
else:
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            # Named, so two processes in one dev box at least agree on the
            # name — and so the fallback is visible in `settings.CACHES`
            # rather than being an absence someone has to know about.
            'LOCATION': 'aiaas-local',
        }
    }

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', REDIS_URL)
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', REDIS_URL)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'

# Periodic work. Run with:  celery -A workflow_backend beat -l info
# The HITL sweep is also runnable without a broker as
# `manage.py send_hitl_reminders`, which is how local dev and cron-only
# deployments drive it — see notifications/reminders.py.
HITL_REMINDER_SWEEP_SECONDS = int(os.environ.get('HITL_REMINDER_SWEEP_SECONDS', '300'))
TRIGGER_SWEEP_SECONDS = int(os.environ.get('TRIGGER_SWEEP_SECONDS', '60'))
RECYCLE_SWEEP_SECONDS = int(os.environ.get('RECYCLE_SWEEP_SECONDS', '3600'))
CELERY_BEAT_SCHEDULE = {
    'sweep-hitl-reminders': {
        'task': 'notifications.sweep_hitl_reminders',
        'schedule': HITL_REMINDER_SWEEP_SECONDS,
    },
    # Every minute: cron's own resolution is one minute, so a slower sweep
    # would make `next_due_at` a suggestion rather than a schedule. Also
    # runnable as `manage.py run_due_triggers` — see agents/sweep.py.
    'sweep-due-triggers': {
        'task': 'orchestrator.sweep_triggers',
        'schedule': TRIGGER_SWEEP_SECONDS,
    },
    # Hourly: a 30-day retention has no use for minute resolution, and this
    # sweep is the destructive one. Also runnable as
    # `manage.py purge_recycle_bin` — see inference/recycle.py.
    'sweep-recycle-bin': {
        'task': 'inference.sweep_recycle_bin',
        'schedule': RECYCLE_SWEEP_SECONDS,
    },
    # Runs whose process went away. Slower than the trigger sweep because a
    # run is only judged orphaned well past its own wall-clock limit, so
    # checking oftener would find the same nothing. Also runnable as
    # `manage.py recover_runs` — see agents/recovery.py, where the reason that
    # second path matters is sharpest: this is the recovery for a dead
    # process, so a broker-only design would be missing exactly when needed.
    'recover-orphaned-runs': {
        'task': 'orchestrator.recover_runs',
        'schedule': RUN_RECOVERY_SWEEP_SECONDS,
    },
}

# How long a trashed folder or document stays restorable before the sweep
# purges it for good. Policy, not a shape cap, so it lives here and is
# env-tunable — and the API reports it as `purges_after_days` rather than
# letting any client hardcode 30.
RECYCLE_BIN_RETENTION_DAYS = int(os.environ.get('RECYCLE_BIN_RETENTION_DAYS', '30'))

# Default wall-clock time for the daily HITL digest, applied to new users.
HITL_DIGEST_DEFAULT_TIME = os.environ.get('HITL_DIGEST_DEFAULT_TIME', '09:00')

# A pending HITL request older than this is treated as abandoned and cancelled
# by the same sweep, so it stops nudging and stops holding its run open. Not
# `HITLRequest.timeout_seconds` — see notifications/reminders.py::ABANDON_AFTER.
HITL_ABANDON_AFTER_DAYS = int(os.environ.get('HITL_ABANDON_AFTER_DAYS', '7'))

RUN_WORKFLOWS_ASYNC = os.environ.get('RUN_WORKFLOWS_ASYNC', 'False') == 'True'

CREDENTIAL_ENCRYPTION_KEY = os.environ.get('CREDENTIAL_ENCRYPTION_KEY')
if not CREDENTIAL_ENCRYPTION_KEY:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("CREDENTIAL_ENCRYPTION_KEY must be set in environment variables.")

GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
GOOGLE_OAUTH_REDIRECT_URI = os.environ.get('GOOGLE_OAUTH_REDIRECT_URI', '')
GOOGLE_OAUTH_LOGIN_SCOPES = [
    'https://www.googleapis.com/auth/userinfo.email',
    'https://www.googleapis.com/auth/userinfo.profile',
    'openid',
]

REST_AUTH = {
    'USE_JWT': True,
    'TOKEN_MODEL': None,
    'JWT_AUTH_COOKIE': 'access_token',
    'JWT_AUTH_REFRESH_COOKIE': 'refresh_token',
    'JWT_AUTH_HTTPONLY': True,
    'USER_DETAILS_SERIALIZER': 'core.serializers.UserSerializer',
}

ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = 'none'

EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True') == 'True'
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', EMAIL_HOST_USER or 'no-reply@aiaas.local')

NOTIFICATIONS_EMAIL_ENABLED = os.environ.get('NOTIFICATIONS_EMAIL_ENABLED', 'True') == 'True'
NOTIFICATIONS_EMAIL_TYPES = _split_env_list(os.environ.get('NOTIFICATIONS_EMAIL_TYPES', ''))
NOTIFICATIONS_EMAIL_SUBJECT_PREFIX = os.environ.get('NOTIFICATIONS_EMAIL_SUBJECT_PREFIX', '[AIAAS]')

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'APP': {
            'client_id': GOOGLE_OAUTH_CLIENT_ID,
            'secret': GOOGLE_OAUTH_CLIENT_SECRET,
            'key': '',
        },
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
    }
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'sensitive_data': {
            '()': 'core.safety.security.SensitiveDataFilter',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'filters': ['sensitive_data'],
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        # Django's DEFAULT_LOGGING gives 'django' its own console handler and
        # leaves propagate=True, so every record it handles was ALSO re-handled
        # by root's console handler above — every request logged twice.
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
        # daphne's runserver access log ("HTTP GET /path 200 [0.01, ip]").
        # RequestLoggingMiddleware already logs the same request with the user
        # id attached, so this one is pure duplication in dev.
        'django.channels.server': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

CANVAS_AGENT_MODEL = os.environ.get('CANVAS_AGENT_MODEL', 'nvidia/nemotron-3-super-120b-a12b')

# NOTE: OpenRouter API keys are loaded per-user from the encrypted `credentials`
# vault (slug 'openrouter'). Do not reintroduce an OPEN_ROUTER_KEY setting.
IMAGINE_AGENT_MODEL = os.environ.get('IMAGINE_AGENT_MODEL', 'openrouter/openai/gpt-4o-mini')
IMAGINE_HITL_COST_THRESHOLD = float(os.environ.get('IMAGINE_HITL_COST_THRESHOLD', '0.10'))


# ==================== Evaluation ====================
# Default provider/model for the `llm_judge` grader when a case does not name
# one. Left blank, `llm.access` resolves the provider's own default model. A
# judge deliberately defaults to a *different* call than the agent under test:
# same-model self-grading is the one configuration where a rubric failure and
# an agent failure cannot be told apart.
EVAL_JUDGE_PROVIDER = os.environ.get('EVAL_JUDGE_PROVIDER', 'openrouter')
EVAL_JUDGE_MODEL = os.environ.get('EVAL_JUDGE_MODEL', '')


# ==================== Context curation ====================
# The model that folds an agent run's earlier steps into a running note when the
# transcript outgrows the window (`chat/turn/curation.py`, the agent's
# `recursiveContext` toggle). Deliberately a small, cheap model and deliberately
# not the agent's own: a forty-turn run on an expensive model would otherwise
# pay full rate to compress itself, and the fold is an extractive job — keep
# these figures, drop this prose — that a large model is not better at.
#
# Left blank, the run's own provider/model is used. That is the fallback rather
# than the default because a fold that cannot run at all is worse than one that
# costs a little: without it the transcript stays whole and `clamp_input` drops
# the oldest segments with no summary behind them.
# NVIDIA on purpose: it is the provider the platform ships a key for
# (`credentials.resolution.PLATFORM_KEY_ENV`), so the fold works on a fresh
# install and for a user who has connected nothing of their own. Every other
# provider would make the fold depend on a credential the user may not have —
# and a context mechanism that silently stops working when a key is missing is
# worse than one that costs a little, because the run just starts losing its
# oldest steps again with no summary behind them.
CONTEXT_SUMMARY_PROVIDER = os.environ.get('CONTEXT_SUMMARY_PROVIDER', 'nvidia')
CONTEXT_SUMMARY_MODEL = os.environ.get(
    'CONTEXT_SUMMARY_MODEL', 'nvidia/nemotron-3.5-lightning-30b-a3b'
)

# Which model writes an agent's configuration from the builder's chat pane
# (`agents/views/builder.py`). Blank falls through to the context-summary pair
# above, for the same reason it exists: that one runs on the platform's own key,
# so the builder still configures agents for a user who has connected nothing.
# The agent's *own* model is tried first regardless — this is the fallback.
AGENT_BUILDER_PROVIDER = os.environ.get('AGENT_BUILDER_PROVIDER', '')
AGENT_BUILDER_MODEL = os.environ.get('AGENT_BUILDER_MODEL', '')
