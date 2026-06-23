"""
Django settings for monkey project.
"""

import sys
from datetime import timedelta
from pathlib import Path

import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.

BASE_DIR = Path(__file__).resolve().parent.parent


# Initialize environ

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
)

environ.Env.read_env(BASE_DIR / ".env")


# Security

DEBUG = env("DJANGO_DEBUG")

SECRET_KEY = env("DJANGO_SECRET")

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=["127.0.0.1", "localhost"],
)

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=["http://localhost:5173"],
)

CORS_ALLOW_CREDENTIALS = env.bool(
    "CORS_ALLOW_CREDENTIALS",
    default=True,
)


# Application definition

INSTALLED_APPS = [
    # daphne must come first so it owns the runserver command (ASGI dev server)
    "daphne",
    "channels",
    # vendor apps
    "rest_framework",
    "django_filters",
    "corsheaders",
    "django_celery_beat",
    "django_celery_results",
    # django apps
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # user apps
    "market",
    "monkey",
    "feedback",
    "analytics",
]


# Celery configuration

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://127.0.0.1:6379/0")

CELERY_RESULT_BACKEND = "django-db"

# Record task name + args + kwargs on every TaskResult row for easy debugging.
CELERY_RESULT_EXTENDED = True

# Emit the STARTED state when a worker picks up a task so TaskResult.date_started
# is populated — lets us measure queue-wait time (date_started - date_created) and
# execution time (date_done - date_started).
CELERY_TASK_TRACK_STARTED = True

CELERY_ACCEPT_CONTENT = ["application/json"]

CELERY_TASK_SERIALIZER = "json"

CELERY_RESULT_SERIALIZER = "json"

CELERY_TIMEZONE = "Asia/Seoul"

# Honor the Django LOGGING config below instead of Celery's own root-logger setup.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

CELERY_TASK_DEFAULT_QUEUE = "default"

# KIS tasks are split by rate limit so the real account's higher budget (~18 req/s)
# is not blocked by mock-account tasks (~1 req/s per account).
#
# kis_orders  — mock-account tasks (orders, system monkey, finalization, off-hours).
#               Concurrency 1; throughput capped by the mock rate limiter.
# kis_prices  — account-free price polling via the real account (~18 req/s).
#               Concurrency 1; runs independently of order traffic.
# default     — non-KIS tasks (no rate limiter).
CELERY_TASK_ROUTES = {
    # Mock account (~1 req/s): orders and all mock-account maintenance
    "monkey.tasks.run_monkey": {"queue": "kis_orders"},
    "monkey.tasks.run_monkeys": {"queue": "kis_orders"},
    "monkey.tasks.run_system_monkey": {"queue": "kis_orders"},
    "monkey.tasks.finalize_filled_orders": {"queue": "kis_orders"},
    "monkey.tasks.reconcile_executions": {"queue": "kis_orders"},
    "monkey.tasks.update_token": {"queue": "kis_orders"},
    "monkey.tasks.check_holiday": {"queue": "kis_orders"},
    "monkey.tasks.auto_create_monkeys": {"queue": "kis_orders"},
    # Real account (~18 req/s): price polling via get_account_free_client()
    "monkey.tasks.get_stock_price": {"queue": "kis_prices"},
    "monkey.tasks.update_held_stock_prices": {"queue": "kis_prices"},
    "monkey.tasks.update_all_stock_prices": {"queue": "kis_prices"},
    # Non-KIS: cull + snapshot + index baseline
    "monkey.tasks.daily_maintenance": {"queue": "default"},
}

CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Minimum spacing between any two KIS HTTP requests, per account. Paper/mock
# trading caps at ~1/sec; real trading allows ~18/sec (used for account-free
# tasks like price polling). The limiter is keyed per account, so each account
# gets its own budget (see monkey.kis.kis_throttle / Account.rate_limit_interval).
KIS_MOCK_REQUEST_INTERVAL = env.float("KIS_MOCK_REQUEST_INTERVAL", default=1.1)

KIS_REAL_REQUEST_INTERVAL = env.float("KIS_REAL_REQUEST_INTERVAL", default=1.0 / 18)

# How many times to retry a transient KIS failure (5xx / timeout / rate-limit).
KIS_MAX_RETRIES = env.int("KIS_MAX_RETRIES", default=3)

# (connect, read) timeout for every KIS HTTP request.
KIS_REQUEST_TIMEOUT = (5, 15)

# Don't throttle/sleep during the test suite.
if "test" in sys.argv:
    KIS_MOCK_REQUEST_INTERVAL = 0
    KIS_REAL_REQUEST_INTERVAL = 0


# Middleware

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"

ASGI_APPLICATION = "core.asgi.application"


# Channels — live WebSocket layer. Uses Redis (a separate db index from the
# Celery broker at db 0 to avoid key collisions). Tests use an in-memory layer.

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [
                env("CHANNEL_LAYERS_REDIS_URL", default="redis://127.0.0.1:6379/2")
            ],
        },
    },
}

if "test" in sys.argv:
    CHANNEL_LAYERS = {
        "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
    }


# Database

DATABASES = {
    "default": env.db("DJANGO_DATABASE"),
}

# Reuse connections and drop dead ones (matters under PostgreSQL + multiple workers).
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DJANGO_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True


# Logging — rotating files under _logs/ (created on import; safe to call repeatedly).

LOGS_DIR = BASE_DIR / "_logs"
LOGS_DIR.mkdir(exist_ok=True)


def _rotating_file(filename):
    return {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": str(LOGS_DIR / filename),
        "maxBytes": 10 * 1024 * 1024,  # 10 MB per file
        "backupCount": 10,
        "formatter": "verbose",
        "encoding": "utf-8",
    }


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "django_file": _rotating_file("django.log"),
        "celery_file": _rotating_file("celery.log"),
        "kis_file": _rotating_file("kis.log"),
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console", "django_file"],
            "level": "INFO",
            "propagate": False,
        },
        "celery": {
            "handlers": ["console", "celery_file"],
            "level": "INFO",
            "propagate": False,
        },
        "monkey": {
            "handlers": ["console", "celery_file"],
            "level": "INFO",
            "propagate": False,
        },
        "market": {
            "handlers": ["console", "celery_file"],
            "level": "INFO",
            "propagate": False,
        },
        "monkey.kis": {
            "handlers": ["console", "kis_file"],
            "level": "INFO",
            "propagate": False,
        },
    },
}


# Password validation

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization

LANGUAGE_CODE = "ko-KR"

TIME_ZONE = "Asia/Seoul"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)

STATIC_URL = "static/"

STATIC_ROOT = BASE_DIR / "_static"


# KIS Open API
#
# Credentials (app key/secret/CANO) are no longer read from the environment —
# they live encrypted in the database (monkey.models.Account) and are registered
# via the manage UI. Only the cross-account knobs remain here.

# Fernet key used by monkey.fields.EncryptedTextField to encrypt KIS app
# key/secret at rest. Generate with `Fernet.generate_key()` and back it up
# securely — losing it makes every stored credential unrecoverable.
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default="")

# Tests don't load a real .env, so supply a throwaway Fernet key so the
# EncryptedTextField round-trips. Never used outside the test suite.
if "test" in sys.argv and not FIELD_ENCRYPTION_KEY:
    from cryptography.fernet import Fernet

    FIELD_ENCRYPTION_KEY = Fernet.generate_key().decode()

KIS_TOKEN_REFRESH_MARGIN_SECONDS = env.int(
    "KIS_TOKEN_REFRESH_MARGIN_SECONDS",
    default=300,
)


# Email

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

EMAIL_HOST = env("EMAIL_HOST", default="smtp-relay.brevo.com")

EMAIL_PORT = env.int("EMAIL_PORT", default=587)

EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)

EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")

EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="noreply@monkey.whalebeta.com")

FEEDBACK_ADMIN_EMAIL = env("FEEDBACK_ADMIN_EMAIL", default="")

# Public site URL — used to build absolute links/logo URLs in HTML emails.
SITE_URL = env("SITE_URL", default="https://monkey.whalebeta.com")


# Django REST Framework

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "feedback-create": "5/hour",
    },
}


# Simple JWT configuration

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}
