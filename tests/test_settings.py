import importlib
import sys

import environ
import pytest
from django.core.exceptions import ImproperlyConfigured


@pytest.fixture
def fresh(monkeypatch):
    """Import a settings module from scratch, ignoring any local .env file."""
    monkeypatch.setattr(environ.Env, "read_env", staticmethod(lambda *a, **k: None))
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db.example.com:5432/app")

    def load(name):
        for module in (f"config.settings.{name}", "config.settings.base"):
            monkeypatch.delitem(sys.modules, module, raising=False)
        return importlib.import_module(f"config.settings.{name}")

    return load


def test_prod_refuses_to_start_without_secret_key(monkeypatch, fresh):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ImproperlyConfigured):
        fresh("prod")


def test_prod_forces_secure_settings(monkeypatch, fresh):
    monkeypatch.setenv("SECRET_KEY", "x" * 60)
    monkeypatch.setenv("DEBUG", "True")
    prod = fresh("prod")
    assert prod.DEBUG is False
    assert prod.SESSION_COOKIE_SECURE and prod.CSRF_COOKIE_SECURE
    assert prod.SECURE_HSTS_SECONDS >= 31536000
    assert prod.SECURE_SSL_REDIRECT is True
    assert prod.SECURE_REDIRECT_EXEMPT == [r"^api/health/$"]
    db = prod.DATABASES["default"]
    assert db["OPTIONS"]["sslmode"] == "require"
    assert db["CONN_HEALTH_CHECKS"] is True


def test_loopback_targets_cannot_be_enabled_in_prod(monkeypatch, fresh):
    """The e2e-only loopback switch: off in prod whatever the environment says."""
    monkeypatch.setenv("SECRET_KEY", "x" * 60)
    monkeypatch.setenv("DEBUG", "True")
    monkeypatch.setenv("SSRF_ALLOW_LOOPBACK", "True")
    prod = fresh("prod")
    assert prod.SSRF_ALLOW_LOOPBACK is False
    assert prod.DEBUG is False  # and the switch is ignored without DEBUG anyway


def test_loopback_switch_is_off_by_default_in_dev_and_base(monkeypatch, fresh):
    monkeypatch.delenv("SSRF_ALLOW_LOOPBACK", raising=False)
    assert fresh("dev").SSRF_ALLOW_LOOPBACK is False
    assert fresh("base").SSRF_ALLOW_LOOPBACK is False


def test_worker_and_check_defaults(monkeypatch, fresh):
    for var in ("WORKER_THREADS", "JOB_LEASE_SECONDS", "ALERT_MAX_ATTEMPTS"):
        monkeypatch.delenv(var, raising=False)
    base = fresh("base")
    assert base.WORKER_THREADS == 4
    assert base.SCHEDULER_INTERVAL_SECONDS == 10
    assert base.JOB_SUCCEEDED_RETENTION_DAYS == 7
    assert base.ALERT_MAX_ATTEMPTS == 5
    assert base.ALERT_TIMEOUT_SECONDS == 5
    assert base.MONITOR_DEFAULT_INTERVAL_SECONDS == 60
    assert base.LOGGING["loggers"]["httpx"]["level"] == "WARNING"
