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

    return {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': Path(os.environ.get('SQLITE_PATH', str(BASE_DIR / 'db.sqlite3'))),
        'OPTIONS': {'timeout': 20},
    }


SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-default-key-change-in-production')
DEBUG = os.environ.get('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = [
    'localhost',
    '127.0.0.1',
    '.ngrok-free.app',
    '.ngrok-free.dev',
    os.environ.get('PUBLIC_URL', '').replace('https://', '').replace('http://', '').split('/')[0],
]
ALLOWED_HOSTS.extend(_split_env_list(os.environ.get('ALLOWED_HOSTS', '')))
ALLOWED_HOSTS = [h for h in ALLOWED_HOSTS if h]

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
    'storages',
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'dj_rest_auth',
    'dj_rest_auth.registration',
    'core',
    'nodes',
    'compiler',
    'executor',
    'orchestrator',
    'credentials',
    'inference',
    'logs',
    'streaming',
    'templates',
    'mcp_integration',
    'skills',
    'chat',
    'browserOS',
    'django_celery_beat',
    'buddy',
    'canvas_agent',
    'notifications',
    'imagine',
]

SITE_ID = 1

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'core.middleware.RequestLoggingMiddleware',
    'core.middleware.InputSanitizationMiddleware',
    'core.middleware.RateLimitHeaderMiddleware',
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

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'rest_framework_simplejwt.authentication.JWTAuthentication',
        'core.authentication.APIKeyAuthentication',
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
        'password_reset': '10/hour',
        'password_change': '10/hour',
        'guest_chat_min': '3/minute',
        'guest_chat_hour': '15/hour',
        'guest_chat_day': '50/day',
    },
}

# --- Guest chat (anonymous NVIDIA NIM demo) ---
NVIDIA_API_KEY = os.environ.get('NVIDIA_API_KEY', '')
NVIDIA_GUEST_MODEL = os.environ.get(
    'NVIDIA_GUEST_MODEL',
    'nvidia/llama-3.3-nemotron-super-49b-v1',
)
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

try:
    from workflow_backend.thresholds import (
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

CELERY_BROKER_URL = os.environ.get('CELERY_BROKER_URL', REDIS_URL)
CELERY_RESULT_BACKEND = os.environ.get('CELERY_RESULT_BACKEND', REDIS_URL)
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'

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
            '()': 'core.security.SensitiveDataFilter',
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
}

CANVAS_AGENT_MODEL = os.environ.get('CANVAS_AGENT_MODEL', 'openai/gpt-4o-mini')

OPEN_ROUTER_KEY = os.environ.get('OPEN_ROUTER_KEY', '')
IMAGINE_AGENT_MODEL = os.environ.get('IMAGINE_AGENT_MODEL', 'openrouter/openai/gpt-4o-mini')
IMAGINE_HITL_COST_THRESHOLD = float(os.environ.get('IMAGINE_HITL_COST_THRESHOLD', '0.10'))
