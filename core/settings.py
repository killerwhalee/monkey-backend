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
]


# Celery configuration

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://127.0.0.1:6379/0")

CELERY_RESULT_BACKEND = "django-db"

# Record task name + args + kwargs on every TaskResult row for easy debugging.
CELERY_RESULT_EXTENDED = True

CELERY_ACCEPT_CONTENT = ["application/json"]

CELERY_TASK_SERIALIZER = "json"

CELERY_RESULT_SERIALIZER = "json"

CELERY_TIMEZONE = "Asia/Seoul"

# Honor the Django LOGGING config below instead of Celery's own root-logger setup.
CELERY_WORKER_HIJACK_ROOT_LOGGER = False

CELERY_TASK_DEFAULT_QUEUE = "default"

# Group tasks by traffic/timing. Every KIS-touching task shares the one global
# rate limiter (see monkey.kis); the queues exist for isolation/prioritization,
# not parallelism (KIS paper trading caps requests at ~1/sec per account).
CELERY_TASK_ROUTES = {
    # market-open, very high traffic
    "monkey.tasks.run_monkey": {"queue": "kis_orders"},
    "monkey.tasks.run_monkeys": {"queue": "kis_orders"},
    "monkey.tasks.get_stock_price": {"queue": "kis_orders"},
    # market-open, low traffic but important
    "monkey.tasks.update_held_stock_prices": {"queue": "kis_maintenance"},
    "monkey.tasks.liquidate_orphaned_holdings": {"queue": "kis_maintenance"},
    # runs while the market is closed
    "monkey.tasks.reconcile_executions": {"queue": "kis_offhours"},
    "monkey.tasks.update_token": {"queue": "kis_offhours"},
    "monkey.tasks.check_holiday": {"queue": "kis_offhours"},
    "monkey.tasks.auto_create_monkeys": {"queue": "kis_offhours"},
}

CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Minimum spacing between any two KIS HTTP requests (paper trading: ~1/sec).
KIS_MIN_REQUEST_INTERVAL = env.float("KIS_MIN_REQUEST_INTERVAL", default=1.1)

# How many times to retry a transient KIS failure (5xx / timeout / rate-limit).
KIS_MAX_RETRIES = env.int("KIS_MAX_RETRIES", default=3)

# (connect, read) timeout for every KIS HTTP request.
KIS_REQUEST_TIMEOUT = (5, 15)

# Don't throttle/sleep during the test suite.
if "test" in sys.argv:
    KIS_MIN_REQUEST_INTERVAL = 0


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

KIS_APP_KEY = env("KIS_APP_KEY", default="")

KIS_APP_SECRET = env("KIS_APP_SECRET", default="")

KIS_CANO = env("KIS_CANO", default="")

KIS_API_BASE_URL = env(
    "KIS_API_BASE_URL",
    default="https://openapivts.koreainvestment.com:29443",
)

KIS_ENVIRONMENT = env(
    "KIS_ENVIRONMENT",
    default="virtual",
)

KIS_ACNT_PRDT_CD = env(
    "KIS_ACNT_PRDT_CD",
    default="01",
)

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
