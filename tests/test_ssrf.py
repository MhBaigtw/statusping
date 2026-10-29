"""SSRF protection: URL shape, every blocked address range, and IP pinning. No real network."""

import httpx
import pytest

from apps.core import safe_http
from apps.core.safe_http import BlockedTarget, DNSFailure, blocked_reason, validate_target

from .factories import PUBLIC_IP

BLOCKED_ADDRESSES = [
    ("127.0.0.1", "loopback"),
    ("127.8.9.10", "loopback"),
    ("::1", "loopback"),
    ("10.0.0.1", "private"),
    ("172.16.5.4", "private"),
    ("192.168.1.1", "private"),
    ("fc00::1", "private"),
    ("fd12:3456::1", "private"),
    ("169.254.169.254", "link-local"),  # cloud metadata endpoint
    ("169.254.0.1", "link-local"),
    ("fe80::1", "link-local"),
    ("224.0.0.1", "multicast"),
    ("239.255.255.250", "multicast"),
    ("ff02::1", "multicast"),
    ("0.0.0.0", "unspecified"),
    ("::", "unspecified"),
    ("240.0.0.1", "private"),  # reserved 240/4 (Python lists it as private too)
    ("255.255.255.255", "private"),
    ("100.64.0.1", "not globally routable"),  # CGNAT
    ("198.18.0.1", "private"),  # benchmarking
    ("192.0.2.1", "private"),  # documentation
    # IPv4-mapped IPv6 forms of blocked IPv4 addresses
    ("::ffff:127.0.0.1", "loopback"),
    ("::ffff:10.0.0.1", "private"),
    ("::ffff:169.254.169.254", "link-local"),
    # 6to4 (2002::/16) embedding loopback / metadata
    ("2002:7f00:1::", "embedded IPv4 is loopback"),
    ("2002:a9fe:a9fe::", "embedded IPv4 is link-local"),
    # Teredo, NAT64 (both blocked outright)
    ("2001:0:4136:e378:8000:63bf:3fff:fdd2", "embedded IPv4 is private"),
    ("64:ff9b::7f00:1", "reserved"),
    ("fe80::1%eth0", "link-local"),  # zone id is stripped before classifying
]


@pytest.mark.parametrize("address,reason", BLOCKED_ADDRESSES)
def test_blocked_addresses(address, reason):
    assert blocked_reason(address) == reason


@pytest.mark.parametrize("address", [PUBLIC_IP, "8.8.8.8", "2606:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_are_allowed(address):
    assert blocked_reason(address) is None


@pytest.mark.parametrize("address,_", BLOCKED_ADDRESSES)
def test_hostname_resolving_to_blocked_address_is_rejected(fake_dns, address, _):
    fake_dns["evil.example"] = [address.split("%")[0]]
    with pytest.raises(BlockedTarget, match="non-public address"):
        validate_target("https://evil.example/")


def test_any_private_answer_among_public_ones_is_rejected(fake_dns):
    fake_dns["mixed.example"] = [PUBLIC_IP, "8.8.8.8", "10.0.0.5"]
    with pytest.raises(BlockedTarget):
        validate_target("http://mixed.example/")
    fake_dns["mixed6.example"] = [PUBLIC_IP, "::1"]
    with pytest.raises(BlockedTarget):
        validate_target("http://mixed6.example/")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]:8080/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.1.2.3:8443/",
    ],
)
def test_ip_literal_urls_are_checked_without_dns(url):
    with pytest.raises(BlockedTarget):
        validate_target(url)


@pytest.mark.parametrize(
    "url,message",
    [
        ("ftp://example.com/", "Only http and https"),
        ("file:///etc/passwd", "Only http and https"),
        ("gopher://example.com/", "Only http and https"),
        ("javascript:alert(1)", "Only http and https"),
        ("https://user:pw@example.com/", "credentials"),
        ("https://user@example.com/", "credentials"),
        ("https://example.com:22/", "ports"),
        ("https://example.com:8/", "ports"),
        ("https://example.com:1023/", "ports"),
        ("https://example.com:99999/", "ports"),
        ("https:///path", "no host"),
        ("not a url", "Only http and https"),
    ],
)
def test_bad_url_shapes(url, message):
    with pytest.raises(BlockedTarget, match=message):
        validate_target(url)


@pytest.mark.parametrize("port", [80, 443, 1024, 8080, 65535])
def test_allowed_ports(port):
    target = validate_target(f"http://example.com:{port}/")
    assert target.port == port


def test_allowed_public_host():
    target = validate_target("https://Example.COM/status?x=1")
    assert target.host == "example.com"
    assert target.port == 443
    assert target.addresses == (PUBLIC_IP,)


def test_unresolvable_host_is_a_dns_failure():
    with pytest.raises(DNSFailure):
        validate_target("https://does-not-exist.example/")


def test_loopback_switch_needs_debug(settings):
    settings.SSRF_ALLOW_LOOPBACK = True
    settings.DEBUG = False
    assert blocked_reason("127.0.0.1") == "loopback"
    settings.DEBUG = True
    assert blocked_reason("127.0.0.1") is None
    assert blocked_reason("::1") is None
    # The dev switch covers loopback only, never private or metadata addresses.
    assert blocked_reason("10.0.0.1") == "private"
    assert blocked_reason("169.254.169.254") == "link-local"


# Pinning: the request goes to the validated IP, with the original Host and SNI


def test_send_connects_to_validated_ip_with_original_host_and_sni(http):
    route = http.get(f"https://{PUBLIC_IP}/health").respond(204)
    result = safe_http.send("GET", "https://example.com/health", timeout=5)
    assert result.status_code == 204
    request = route.calls.last.request
    assert request.url.host == PUBLIC_IP
    assert request.headers["Host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"
    assert request.headers["User-Agent"].startswith("StatusPing/")


def test_send_keeps_non_default_port_in_host_header(fake_dns, http):
    route = http.get(f"http://{PUBLIC_IP}:8080/").respond(200)
    safe_http.send("GET", "http://example.com:8080/", timeout=5)
    request = route.calls.last.request
    assert request.headers["Host"] == "example.com:8080"
    assert "sni_hostname" not in request.extensions


def test_send_prefers_ipv4_and_handles_ipv6_only_hosts(fake_dns, http):
    fake_dns["dual.example"] = ["2606:4700::1111", "8.8.8.8"]
    http.get("http://8.8.8.8/").respond(200)
    assert safe_http.send("GET", "http://dual.example/", timeout=5).status_code == 200
    fake_dns["v6.example"] = ["2606:4700::1111"]
    route = http.get("http://[2606:4700::1111]/").respond(200)
    safe_http.send("GET", "http://v6.example/", timeout=5)
    assert route.called


def test_send_does_not_follow_redirects(http):
    http.get(f"https://{PUBLIC_IP}/old").respond(302, headers={"Location": "http://127.0.0.1/"})
    assert safe_http.send("GET", "https://example.com/old", timeout=5).status_code == 302
    assert len(http.calls) == 1


def test_send_revalidates_dns_every_time(fake_dns, http):
    http.get(f"https://{PUBLIC_IP}/").respond(200)
    assert safe_http.send("GET", "https://example.com/", timeout=5).status_code == 200
    fake_dns["example.com"] = ["10.0.0.7"]  # DNS now points inside
    with pytest.raises(BlockedTarget):
        safe_http.send("GET", "https://example.com/", timeout=5)
    assert len(http.calls) == 1


def test_send_ignores_proxy_environment(monkeypatch, http):
    monkeypatch.setenv("HTTPS_PROXY", "http://10.0.0.1:3128")
    http.get(f"https://{PUBLIC_IP}/").respond(200)
    assert safe_http.send("GET", "https://example.com/", timeout=5).status_code == 200


def test_send_raises_httpx_errors(http):
    http.get(f"https://{PUBLIC_IP}/").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(httpx.TimeoutException):
        safe_http.send("GET", "https://example.com/", timeout=1)


def test_ssl_context_verifies_certificates():
    context = safe_http._ssl_context()
    assert context.check_hostname is True
    assert context.verify_mode.name == "CERT_REQUIRED"
