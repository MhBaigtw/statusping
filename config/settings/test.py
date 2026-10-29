from .base import *  # noqa: F403
from .base import MIDDLEWARE

DEBUG = False
SECRET_KEY = "test-only-insecure-key-long-enough-for-hs256-signing"
ALLOWED_HOSTS = ["testserver", "localhost"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
# Tests never serve static files; skip WhiteNoise's missing-STATIC_ROOT warning.
MIDDLEWARE = [m for m in MIDDLEWARE if not m.startswith("whitenoise")]
# In-memory cache for tests (no cache table needed); tests/conftest.py clears it
# before every test so throttle counters never leak between tests.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
SSRF_ALLOW_LOOPBACK = False
