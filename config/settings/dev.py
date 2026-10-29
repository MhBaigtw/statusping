from .base import *  # noqa: F403
from .base import env

DEBUG = env.bool("DEBUG", default=True)
SECRET_KEY = env("SECRET_KEY", default="dev-only-insecure-key-do-not-use-in-production")
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# Lets scripts/e2e.py point monitors and webhooks at fake servers on 127.0.0.1.
# Ignored unless DEBUG is True (apps/core/safe_http.py); prod.py forces it off.
SSRF_ALLOW_LOOPBACK = env.bool("SSRF_ALLOW_LOOPBACK", default=False)
