"""Live verification of a deployed StatusPing (Part F). About 10 minutes.

    .\\.venv\\Scripts\\python.exe scripts\\verify_live.py https://statusping-xxxx.onrender.com

Checks, against the real deployment:
  1. /api/health/live/ and /api/health/ (fresh worker heartbeat), /api/docs/, /api/schema/,
     and the demo status page as JSON and HTML.
  2. Registers a throwaway user (random password, never printed) and org. It adds a monitor on
     a real public URL and one on https://httpbin.org/status/500, plus a Discord channel with
     a well-formed but INVALID webhook, because no real webhook is used (D29/D31). Discord
     answers 4xx, so the delivery log must show the attempt as a permanent failure, not
     retried.
  3. The 500 monitor must open an incident after `failure_threshold` checks. The healthy
     monitor must be checked about once per interval. Then the 500 monitor is pointed at
     /status/200 and must resolve.
  4. Proxy depth (NUM_PROXIES): 11 failed logins, each with a different forged
     X-Forwarded-For, must hit 429. If they don't, the forged header is being trusted.
  5. Cleanup: deletes the monitors and the channel.

The monitor checks keep the instance awake for the run; that is this script's own traffic,
not a keep-alive pinger.
"""

import secrets
import sys
import time
from datetime import UTC, datetime

import httpx

FAILURE_THRESHOLD = 2
RECOVERY_THRESHOLD = 2
INVALID_DISCORD_WEBHOOK = "https://discord.com/api/webhooks/0/statusping-live-verification-invalid"


def say(message):
    print(f"[{datetime.now(UTC):%H:%M:%S}] {message}", flush=True)


def wait_until(predicate, timeout, what, every=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    raise SystemExit(f"FAILED: timed out after {timeout}s waiting for {what}")


def main(base):
    base = base.rstrip("/")
    client = httpx.Client(base_url=base, timeout=90)

    started = time.monotonic()
    live = client.get("/api/health/live/")
    say(f"/api/health/live/ {live.status_code} in {time.monotonic() - started:.1f}s: {live.json()}")
    assert live.status_code == 200
    health = client.get("/api/health/")
    say(f"/api/health/ {health.status_code}: {health.json()}")
    assert health.status_code == 200 and health.json()["worker"]["last_seen_seconds_ago"] < 30
    for path in ("/api/docs/", "/api/schema/"):
        response = client.get(path)
        say(f"{path} {response.status_code} ({len(response.content)} bytes)")
        assert response.status_code == 200
    status = client.get("/status/demo/")
    say(f"/status/demo/ {status.status_code}")
    if status.status_code == 200:
        page = status.json()
        say(
            f"  overall={page['status']}; "
            + "; ".join(
                f"{m['name']}: {m['status']}, 24h={m['uptime']['24h']}" for m in page["monitors"]
            )
        )
        html = client.get("/status/demo")
        say(f"/status/demo (HTML) {html.status_code}, {html.headers.get('content-type')}")
        assert html.status_code == 200 and "text/html" in html.headers["content-type"]

    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    email = f"live-verify-{stamp}@example.com"
    password = secrets.token_urlsafe(18)
    response = client.post("/api/auth/register/", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    token = client.post("/api/auth/token/", json={"email": email, "password": password})
    assert token.status_code == 200, token.text
    client.headers["Authorization"] = f"Bearer {token.json()['access']}"
    slug = f"live-verify-{stamp}"
    response = client.post("/api/orgs/", json={"name": f"Live verify {stamp}", "slug": slug})
    assert response.status_code == 201, response.text
    org = f"/api/orgs/{slug}"
    say(f"registered {email}, org {slug}")

    def create_monitor(name, url):
        response = client.post(
            f"{org}/monitors/",
            json={
                "name": name,
                "url": url,
                "interval_seconds": 60,
                "timeout_seconds": 10,
                "failure_threshold": FAILURE_THRESHOLD,
                "recovery_threshold": RECOVERY_THRESHOLD,
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    healthy_id = create_monitor("Example homepage", "https://example.com/")
    failing_id = create_monitor("httpbin 500", "https://httpbin.org/status/500")
    channel = client.post(
        f"{org}/alert-channels/",
        json={
            "kind": "discord",
            "name": "Invalid Discord (verification)",
            "webhook_url": INVALID_DISCORD_WEBHOOK,
        },
    )
    assert channel.status_code == 201, channel.text
    channel_id = channel.json()["id"]
    say(f"monitors {healthy_id} (example.com) and {failing_id} (httpbin 500); channel {channel_id}")

    def checks(monitor_id):
        return client.get(f"{org}/monitors/{monitor_id}/checks/").json()["results"]

    def incidents(status):
        return client.get(f"{org}/incidents/?status={status}").json()["results"]

    try:
        opened = wait_until(lambda: incidents("open"), 420, "incident to open")
        failing = checks(failing_id)
        say(
            f"incident {opened[0]['id']} OPEN, cause={opened[0]['cause']}, after "
            f"{len([c for c in failing if not c['ok']])} failed checks: "
            + ", ".join(
                f"{c['checked_at'][11:19]} {c['status_code']} {c['response_ms']}ms"
                for c in reversed(failing)
            )
        )
        assert len([c for c in failing if not c["ok"]]) == FAILURE_THRESHOLD

        def delivery(event):
            rows = client.get(f"{org}/incidents/{opened[0]['id']}/deliveries/").json()["results"]
            done = [d for d in rows if d["event"] == event and d["status"] != "pending"]
            return done[0] if done else None

        sent = wait_until(lambda: delivery("opened"), 180, "opened delivery attempt")
        say(
            f"delivery opened: status={sent['status']} attempts={sent['attempts']} "
            f"last_error={sent['last_error']!r}"
        )
        assert sent["status"] == "failed" and sent["attempts"] == 1  # 4xx: permanent, no retry

        response = client.patch(
            f"{org}/monitors/{failing_id}/", json={"url": "https://httpbin.org/status/200"}
        )
        assert response.status_code == 200, response.text
        say("pointed the failing monitor at /status/200")
        wait_until(lambda: not incidents("open"), 420, "incident to resolve")
        resolved = incidents("resolved")[0]
        say(f"incident {resolved['id']} RESOLVED at {resolved['resolved_at']}")
        gone = wait_until(lambda: delivery("resolved"), 180, "resolved delivery attempt")
        say(f"delivery resolved: status={gone['status']} attempts={gone['attempts']}")

        healthy = checks(healthy_id)
        times = sorted(datetime.fromisoformat(c["checked_at"]) for c in healthy)
        gaps = [round((b - a).total_seconds()) for a, b in zip(times, times[1:], strict=False)]
        say(
            f"example.com: {len(healthy)} checks, all ok={all(c['ok'] for c in healthy)}, "
            f"gaps between checks (s): {gaps}, response_ms: "
            f"{[c['response_ms'] for c in healthy]}"
        )
        assert healthy and all(c["ok"] for c in healthy)
        assert all(50 <= gap <= 75 for gap in gaps), gaps
    finally:
        for monitor_id in (healthy_id, failing_id):
            client.delete(f"{org}/monitors/{monitor_id}/")
        client.delete(f"{org}/alert-channels/{channel_id}/")
        say("cleaned up: monitors and channel deleted")

    anon = httpx.Client(base_url=base, timeout=30)
    codes = []
    for n in range(11):
        response = anon.post(
            "/api/auth/token/",
            json={"email": email, "password": "wrong-password"},
            headers={"X-Forwarded-For": f"203.0.113.{n + 1}"},
        )
        codes.append(response.status_code)
    say(f"11 failed logins with forged X-Forwarded-For: {codes}")
    assert codes[-1] == 429, "forged X-Forwarded-For dodged the login throttle"
    say("PASSED")


if __name__ == "__main__":
    main(sys.argv[1])
