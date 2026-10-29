"""Settings shared by every environment. Secrets and the database come from env."""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
if (BASE_DIR / ".env").exists():
    environ.Env.read_env(BASE_DIR / ".env")

DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    "apps.core",
    "apps.accounts",
    "apps.organizations",
    "apps.monitors",
    "apps.jobs",
    "apps.alerts",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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

DATABASES = {"default": env.db("DATABASE_URL")}

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPagination",
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.exceptions.exception_handler",
    # Scoped throttles. login/token_refresh/register are keyed by client IP, writes by user.
    "DEFAULT_THROTTLE_RATES": {
        "login": env("THROTTLE_LOGIN_RATE", default="10/min"),
        "token_refresh": env("THROTTLE_REFRESH_RATE", default="10/min"),
        "register": env("THROTTLE_REGISTER_RATE", default="5/hour"),
        "writes": env("THROTTLE_WRITE_RATE", default="60/min"),
    },
    # How many proxies sit in front of the app; 0 = use REMOTE_ADDR, ignore X-Forwarded-For.
    "NUM_PROXIES": env.int("NUM_PROXIES", default=0),
}

# Throttle counters must be shared by every web process, otherwise each process keeps
# its own count and the effective limit multiplies. The database cache is shared and
# needs no extra service. Create its table with `manage.py createcachetable`.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "django_cache",
    }
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=15)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=1)),
    "UPDATE_LAST_LOGIN": False,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "StatusPing API",
    "DESCRIPTION": "Multi-tenant uptime monitoring: HTTP monitors, a Postgres-backed job queue, "
    "incidents with flap protection, and webhook alerts.",
    "VERSION": "0.2.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "MembershipRoleEnum": "apps.organizations.models.Membership.Role",
        "MonitorStatusEnum": "apps.monitors.models.Monitor.Status",
        "CheckErrorEnum": "apps.monitors.models.CheckResult.Error",
        "AlertEventEnum": "apps.alerts.models.AlertDelivery.Event",
        "DeliveryStatusEnum": "apps.alerts.models.AlertDelivery.Status",
    },
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "apps": {"level": "INFO"},
        # httpx logs every request URL at INFO. Webhook URLs are secrets, so keep
        # these libraries at WARNING (tested in test_alerts.py).
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
}

# Monitors and checks
MONITORS_PER_ORG = env.int("MONITORS_PER_ORG", default=20)
MONITOR_DEFAULT_INTERVAL_SECONDS = env.int("MONITOR_DEFAULT_INTERVAL_SECONDS", default=60)
MONITOR_DEFAULT_TIMEOUT_SECONDS = env.int("MONITOR_DEFAULT_TIMEOUT_SECONDS", default=5)
CHECK_USER_AGENT = env(
    "CHECK_USER_AGENT", default="StatusPing/0.2 (+https://github.com/MhBaigtw/statusping)"
)
DISPATCH_BATCH_SIZE = env.int("DISPATCH_BATCH_SIZE", default=100)

# Worker, scheduler and job queue
WORKER_THREADS = env.int("WORKER_THREADS", default=4)
WORKER_POLL_SECONDS = env.float("WORKER_POLL_SECONDS", default=1.0)
WORKER_SHUTDOWN_TIMEOUT_SECONDS = env.float("WORKER_SHUTDOWN_TIMEOUT_SECONDS", default=30.0)
WORKER_HEARTBEAT_SECONDS = env.float("WORKER_HEARTBEAT_SECONDS", default=5.0)
WORKER_HEARTBEAT_STALE_SECONDS = env.int("WORKER_HEARTBEAT_STALE_SECONDS", default=30)
SCHEDULER_INTERVAL_SECONDS = env.float("SCHEDULER_INTERVAL_SECONDS", default=10.0)
JOB_LEASE_SECONDS = env.int("JOB_LEASE_SECONDS", default=60)
JOB_DEFAULT_MAX_ATTEMPTS = env.int("JOB_DEFAULT_MAX_ATTEMPTS", default=3)
JOB_RETRY_BASE_SECONDS = env.int("JOB_RETRY_BASE_SECONDS", default=10)
JOB_RETRY_MAX_SECONDS = env.int("JOB_RETRY_MAX_SECONDS", default=900)
JOB_SUCCEEDED_RETENTION_DAYS = env.int("JOB_SUCCEEDED_RETENTION_DAYS", default=7)
JOB_PURGE_BATCH_SIZE = env.int("JOB_PURGE_BATCH_SIZE", default=1000)

# Alerts
ALERT_MAX_ATTEMPTS = env.int("ALERT_MAX_ATTEMPTS", default=5)
ALERT_TIMEOUT_SECONDS = env.float("ALERT_TIMEOUT_SECONDS", default=5.0)
ALERT_WEBHOOK_HOSTS = {
    "discord": ["discord.com", "discordapp.com"],
    "slack": ["hooks.slack.com"],
}

# SSRF: never allow loopback targets outside local development. Only honoured when
# DEBUG is True (see apps/core/safe_http.py), and prod.py forces it off.
SSRF_ALLOW_LOOPBACK = False
