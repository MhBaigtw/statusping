"""Live end-to-end run on this machine (~5 minutes, real timings, real processes).

Starts a fake target server and a fake webhook receiver on 127.0.0.1, then the web
server (runserver) and the worker (run_worker) as separate processes against the dev
database. Then it drives the public API:

    register -> token -> org -> monitor -> alert channel
    target healthy -> flip to 500 -> incident opens after the threshold -> one "opened" alert
    flip back to 200 -> incident resolves after the recovery threshold -> one "resolved" alert

Loopback targets are allowed only through SSRF_ALLOW_LOOPBACK, which is set for the two
child processes here, is ignored unless DEBUG is True, and is forced off in prod settings.
Logs go to e2e-logs/. Nothing secret is printed (the e2e user's password is random).

    .\\.venv\\Scripts\\python.exe scripts\\e2e.py
"""

import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from tests.fake_servers import FakeTarget, FakeWebhook  # noqa: E402

LOGS = BASE / "e2e-logs"
FAILURE_THRESHOLD = 2
RECOVERY_THRESHOLD = 2
WINDOWS = sys.platform == "win32"


def say(message):
    print(f"[{datetime.now(UTC):%H:%M:%S}] {message}", flush=True)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rss_mb(pid):
    """Working set in MB of a process plus its children.

    On Windows a venv's python.exe is a small launcher that starts the real interpreter
    as a child process, so measuring the launcher PID alone reports ~4 MB.
    """
    if WINDOWS:
        query = (
            "Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -eq PID -or "
            "$_.ParentProcessId -eq PID } | Measure-Object WorkingSetSize -Sum | "
            "ForEach-Object { $_.Sum }"
        ).replace("PID", str(pid))
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", query], capture_output=True, text=True
        ).stdout.strip()
        return int(out) / 1024 / 1024 if out.isdigit() else None
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        return None
    return None


class Processes:
    def __init__(self):
        self.procs = {}
        self.peak = {}
        self.last_sample = 0.0

    def start(self, name, args, env):
        log = open(LOGS / f"{name}.log", "w", encoding="utf-8")  # noqa: SIM115
        self.procs[name] = (
            subprocess.Popen(
                [sys.executable, "manage.py", *args],
                cwd=BASE,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
            ),
            log,
        )

    def sample_memory(self, force=False):
        if not force and time.monotonic() - self.last_sample < 10:
            return
        self.last_sample = time.monotonic()
        for name, (proc, _) in self.procs.items():
            mb = rss_mb(proc.pid)
            if mb is not None:
                self.peak[name] = max(self.peak.get(name, 0), mb)

    def stop(self):
        for name, (proc, log) in self.procs.items():
            if proc.poll() is None:
                if WINDOWS:
                    os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                else:
                    proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(40)
            except subprocess.TimeoutExpired:
                proc.kill()
            log.close()
            code = proc.returncode
            # runserver has no Ctrl+Break handler: Windows reports STATUS_CONTROL_C_EXIT.
            note = " (STATUS_CONTROL_C_EXIT, expected)" if code == 0xC000013A else ""
            say(f"{name} exited with code {code}{note}")


def wait_until(predicate, timeout, what, procs, every=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        procs.sample_memory()
        value = predicate()
        if value:
            return value
        time.sleep(every)
    raise SystemExit(f"FAILED: timed out after {timeout}s waiting for {what}")


def main():
    LOGS.mkdir(exist_ok=True)
    port = free_port()
    api = f"http://127.0.0.1:{port}/api"
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "config.settings.dev",
        "DEBUG": "True",
        "SSRF_ALLOW_LOOPBACK": "True",
        "PYTHONUNBUFFERED": "1",
    }
    procs = Processes()
    with FakeTarget(status=200) as target, FakeWebhook() as webhook:
        say(f"fake target on {target.url()}, fake webhook on {webhook.url()}")
        try:
            procs.start("web", ["runserver", f"127.0.0.1:{port}", "--noreload"], env)
            procs.start("worker", ["run_worker", "--threads", "2"], env)
            client = httpx.Client(base_url=api, timeout=10)

            def healthy():
                try:
                    response = client.get("/health/")
                except httpx.HTTPError:
                    return None
                # 503 until the worker's first heartbeat (D27); 200 means web + worker are up.
                return response.json() if response.status_code == 200 else None

            health = wait_until(healthy, 60, "web + worker heartbeat", procs, every=1)
            say(f"health: {health}")

            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
            email = f"e2e-{stamp}@example.com"
            password = secrets.token_urlsafe(16)
            resp = client.post("/auth/register/", json={"email": email, "password": password})
            assert resp.status_code == 201, resp.text
            token = client.post("/auth/token/", json={"email": email.upper(), "password": password})
            client.headers["Authorization"] = f"Bearer {token.json()['access']}"
            say(f"registered {email} and logged in (with an upper-cased email)")

            slug = f"e2e-{stamp}"
            assert (
                client.post("/orgs/", json={"name": f"E2E {stamp}", "slug": slug}).status_code
                == 201
            )
            org = f"/orgs/{slug}"
            monitor = client.post(
                f"{org}/monitors/",
                json={
                    "name": "Fake target",
                    "url": target.url("/health"),
                    "interval_seconds": 60,
                    "timeout_seconds": 3,
                    "failure_threshold": FAILURE_THRESHOLD,
                    "recovery_threshold": RECOVERY_THRESHOLD,
                },
            )
            assert monitor.status_code == 201, monitor.text
            monitor_id = monitor.json()["id"]
            channel = client.post(
                f"{org}/alert-channels/",
                json={
                    "kind": "discord",
                    "name": "Fake Discord",
                    "webhook_url": webhook.url("/hook"),
                },
            )
            assert channel.status_code == 201, channel.text
            say(
                f"org {slug}, monitor {monitor_id}, "
                f"channel masked as {channel.json()['webhook_url_masked']}"
            )

            def checks():
                return client.get(f"{org}/monitors/{monitor_id}/checks/").json()["results"]

            def incidents(status):
                return client.get(f"{org}/incidents/?status={status}").json()["results"]

            first = wait_until(lambda: checks(), 60, "first check", procs)
            say(
                f"first check: ok={first[0]['ok']} status={first[0]['status_code']} "
                f"{first[0]['response_ms']}ms"
            )
            assert first[0]["ok"]

            target.status = 500
            flipped = time.monotonic()
            say("target flipped to 500")
            opened = wait_until(lambda: incidents("open"), 240, "incident to open", procs)
            failing = [c for c in checks() if not c["ok"]]
            say(
                f"incident {opened[0]['id']} OPEN after {time.monotonic() - flipped:.0f}s, "
                f"{len(failing)} failed checks (threshold {FAILURE_THRESHOLD}), "
                f"cause={opened[0]['cause']}"
            )
            assert len(failing) == FAILURE_THRESHOLD, failing
            wait_until(lambda: len(webhook.received) >= 1, 60, "opened alert", procs)
            time.sleep(15)  # any duplicate would arrive in this window
            assert len(webhook.received) == 1, webhook.messages()
            say(f"webhook received exactly 1 message: {webhook.messages()[0]!r}")

            target.status = 200
            flipped = time.monotonic()
            say("target flipped back to 200")
            wait_until(
                lambda: not incidents("open") and incidents("resolved"), 240, "resolve", procs
            )
            say(f"incident RESOLVED after {time.monotonic() - flipped:.0f}s")
            wait_until(lambda: len(webhook.received) >= 2, 60, "resolved alert", procs)
            time.sleep(15)
            messages = webhook.messages()
            assert len(messages) == 2, messages
            assert sum("[DOWN]" in m for m in messages) == 1
            assert sum("[UP]" in m for m in messages) == 1
            say(f"webhook received exactly 1 more message: {messages[1]!r}")

            deliveries = client.get(f"{org}/incidents/{opened[0]['id']}/deliveries/").json()[
                "results"
            ]
            say(
                "deliveries: "
                + ", ".join(
                    f"{d['event']}={d['status']} (attempts {d['attempts']})" for d in deliveries
                )
            )
            say(f"checks recorded: {len(checks())}, fake target hits: {target.hits}")
            procs.sample_memory(force=True)
            # The fake servers die with this script: don't leave a monitor and channel behind
            # that would keep checking a dead port on the next worker run.
            assert client.delete(f"{org}/monitors/{monitor_id}/").status_code == 204
            assert client.delete(f"{org}/alert-channels/{channel.json()['id']}/").status_code == 204
            say("cleaned up: monitor and alert channel deleted")
        finally:
            procs.stop()
    say("peak working set: " + ", ".join(f"{k} {v:.0f} MB" for k, v in procs.peak.items()))
    say(f"PASSED. Logs in {LOGS}")


if __name__ == "__main__":
    main()
