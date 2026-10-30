"""Deploy configuration (Part E2): gunicorn hooks, Render host, Blueprint shape."""

import importlib.util
from pathlib import Path

import pytest
import yaml

BASE = Path(__file__).resolve().parent.parent


def load_gunicorn_conf(monkeypatch, **env):
    for key in ("EMBEDDED_WORKER", "WEB_CONCURRENCY", "PORT", "WORKER_THREADS"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location("gunicorn_conf", BASE / "gunicorn.conf.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gunicorn_defaults(monkeypatch):
    conf = load_gunicorn_conf(monkeypatch)
    assert conf.bind == "0.0.0.0:8000"
    assert conf.workers == 2
    assert conf.EMBEDDED_WORKER is False
    conf.post_worker_init(object())  # no-op without the flag
    conf.worker_exit(None, object())


def test_gunicorn_uses_platform_port_and_concurrency(monkeypatch):
    conf = load_gunicorn_conf(monkeypatch, PORT="10000", WEB_CONCURRENCY="3")
    assert (conf.bind, conf.workers) == ("0.0.0.0:10000", 3)


def test_embedded_worker_runs_in_exactly_one_web_process(monkeypatch):
    conf = load_gunicorn_conf(monkeypatch, EMBEDDED_WORKER="true", WEB_CONCURRENCY="4")
    assert conf.workers == 1  # one gunicorn process -> one job runner

    events = []

    class FakeWorker:
        def __init__(self, threads):
            events.append(("init", threads))

        def start(self):
            events.append("start")

        def stop(self):
            events.append("stop")

        def join(self, timeout):
            events.append(("join", timeout))

    monkeypatch.setattr("apps.jobs.worker.Worker", FakeWorker)

    class GunicornWorker:
        pass

    process = GunicornWorker()
    conf.post_worker_init(process)
    conf.worker_exit(None, process)
    assert events == [("init", 2), "start", "stop", ("join", 25)]


@pytest.mark.parametrize("value", ["1", "TRUE", " yes "])
def test_embedded_worker_flag_spellings(monkeypatch, value):
    assert load_gunicorn_conf(monkeypatch, EMBEDDED_WORKER=value).EMBEDDED_WORKER is True


def test_render_blueprint_shape():
    """Option (c), D31: one free web service that runs the worker inside it."""
    blueprint = yaml.safe_load((BASE / "render.yaml").read_text(encoding="utf-8"))
    (web,) = blueprint["services"]
    assert web["type"] == "web" and web["plan"] == "free"
    # The platform's web health check must not depend on the worker (D27).
    assert web["healthCheckPath"] == "/api/health/live/"
    assert "gunicorn.conf.py" in web["startCommand"]
    assert "migrate" in (BASE / "bin" / "render-build.sh").read_text(encoding="utf-8")
    env = {item["key"]: item for item in web["envVars"]}
    assert env["EMBEDDED_WORKER"]["value"] == "true"
    assert env["DJANGO_SETTINGS_MODULE"]["value"] == "config.settings.prod"
    assert float(env["WORKER_POLL_SECONDS"]["value"]) >= 1
    for secret in ("DATABASE_URL", "DEMO_PASSWORD"):
        assert env[secret] == {"key": secret, "sync": False}  # never a value in the repo
    assert env["SECRET_KEY"] == {"key": "SECRET_KEY", "generateValue": True}


def test_render_hostname_is_trusted_in_prod(monkeypatch):
    import importlib
    import sys

    import environ

    monkeypatch.setattr(environ.Env, "read_env", staticmethod(lambda *a, **k: None))
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@db.example.com:5432/app")
    monkeypatch.setenv("SECRET_KEY", "x" * 60)
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "statusping.onrender.com")
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("CSRF_TRUSTED_ORIGINS", raising=False)
    for module in ("config.settings.prod", "config.settings.base"):
        monkeypatch.delitem(sys.modules, module, raising=False)
    prod = importlib.import_module("config.settings.prod")
    assert prod.ALLOWED_HOSTS == ["statusping.onrender.com"]
    assert prod.CSRF_TRUSTED_ORIGINS == ["https://statusping.onrender.com"]
