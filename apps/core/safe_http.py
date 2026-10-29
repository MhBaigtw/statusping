"""Outbound HTTP that cannot be pointed at internal addresses (SSRF protection).

Every outbound request in StatusPing (monitor checks, webhook alerts) goes through
`send()`. It:

1. Validates the URL syntax: http/https only, no credentials, port 80, 443 or 1024-65535.
2. Resolves the hostname and rejects it if ANY address is non-public: private, loopback,
   link-local (incl. 169.254.169.254), multicast, reserved, unspecified, CGNAT, or an
   IPv4-mapped / 6to4 / Teredo form of one of these.
3. Connects to the validated IP itself ("pinning"). The Host header and the TLS SNI and
   certificate check still use the original hostname. A DNS answer that changes between
   validation and connect (DNS rebinding) therefore cannot redirect the connection, and
   neither can a URL-parser disagreement, because the socket target is the IP we checked.
4. Never follows redirects and ignores proxy environment variables (trust_env=False),
   since a proxy would make the connection target something we did not validate.
5. Enforces ONE total time budget (`timeout`) for the whole request: DNS, TCP connect,
   TLS handshake, writing the request and reading the response headers. httpx timeouts
   are per operation, so a server that drips one header byte at a time would never
   trip them. Here every socket operation gets min(its timeout, time left), and the
   request fails with a timeout once the deadline passes (see _DeadlineStream).
   The response body is never read (only the status code matters), and httpcore caps
   the size of the response head it will buffer.

Tests never touch real DNS: they patch `_getaddrinfo` (see tests/conftest.py).
"""

import ipaddress
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from functools import cache

import certifi
import httpcore
import httpx
from django.conf import settings

ALLOWED_SCHEMES = ("http", "https")


class BlockedTarget(Exception):
    """The URL may not be requested (syntax, scheme, port, credentials or a non-public IP)."""


class DNSFailure(Exception):
    """The hostname did not resolve."""


@dataclass(frozen=True)
class Target:
    url: httpx.URL
    host: str  # ASCII (IDNA) hostname, used for DNS, Host header and TLS SNI
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class Result:
    status_code: int
    headers: httpx.Headers
    elapsed_ms: int


def loopback_allowed():
    """DEV ONLY (scripts/e2e.py): allow 127.0.0.0/8 and ::1. Never true unless DEBUG."""
    return bool(settings.DEBUG and settings.SSRF_ALLOW_LOOPBACK)


def parse_url(url):
    """Check the URL's shape (no DNS). Returns (httpx.URL, ascii host, port)."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as exc:
        raise BlockedTarget("Invalid URL.") from exc
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise BlockedTarget("Only http and https URLs are allowed.")
    if parsed.userinfo:
        raise BlockedTarget("URLs must not contain credentials.")
    host = parsed.raw_host.decode("ascii").lower()
    if not host:
        raise BlockedTarget("The URL has no host.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in (80, 443) and not 1024 <= port <= 65535:
        raise BlockedTarget("Only ports 80, 443 and 1024-65535 are allowed.")
    return parsed, host, port


def _getaddrinfo(host, port):
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


# getaddrinfo cannot be given a timeout, so lookups run here and the caller stops waiting
# at its deadline. A lookup that hangs keeps one of these threads until the OS gives up.
_dns_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="dns")


def resolve(host, port, *, deadline=None):
    """All addresses the host resolves to (an IP literal resolves to itself).

    With a `deadline` (time.monotonic() value), raises httpx.ConnectTimeout if the
    lookup has not finished by then.
    """
    try:
        ipaddress.ip_address(host)
        return (host,)
    except ValueError:
        pass
    try:
        if deadline is None:
            infos = _getaddrinfo(host, port)
        else:
            future = _dns_pool.submit(_getaddrinfo, host, port)
            try:
                infos = future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                raise httpx.ConnectTimeout("DNS lookup exceeded the time budget.") from None
    except (OSError, UnicodeError) as exc:  # socket.gaierror is an OSError
        raise DNSFailure(f"Could not resolve {host}.") from exc
    addresses = tuple(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise DNSFailure(f"Could not resolve {host}.")
    return addresses


def _classify(ip):
    """Why this address is not public, or None if it is a normal public address."""
    if ip.is_loopback:
        return "loopback"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if not ip.is_global:  # e.g. 100.64.0.0/10 (CGNAT)
        return "not globally routable"
    return None


def blocked_reason(address):
    """Why connecting to `address` is not allowed, or None if it is allowed."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])  # drop an IPv6 zone id
    if ip.version == 6 and ip.ipv4_mapped is not None:
        # ::ffff:a.b.c.d is literally the IPv4 host a.b.c.d.
        ip = ip.ipv4_mapped
    elif ip.version == 6:
        # Other IPv6 forms that embed an IPv4 address: both parts must be public.
        embedded = ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None and (reason := _classify(embedded)):
            return f"embedded IPv4 is {reason}"
    if ip.is_loopback and loopback_allowed():
        return None
    return _classify(ip)


def validate_target(url, *, deadline=None):
    """Full validation: URL shape, DNS, and every resolved address. Returns a Target."""
    parsed, host, port = parse_url(url)
    addresses = resolve(host, port, deadline=deadline)
    for address in addresses:
        if reason := blocked_reason(address):
            raise BlockedTarget(f"{host} resolves to a non-public address ({reason}).")
    return Target(url=parsed, host=host, port=port, addresses=addresses)


def _pick_address(addresses):
    """Prefer IPv4: it works on every network this runs on; IPv6 often does not."""
    for address in addresses:
        if ":" not in address:
            return address
    return addresses[0]


@cache
def _ssl_context():
    return ssl.create_default_context(cafile=certifi.where())


class _DeadlineStream(httpcore.NetworkStream):
    """A socket stream whose every operation is clamped to the time left before `deadline`."""

    def __init__(self, stream, deadline):
        self._stream = stream
        self._deadline = deadline

    def _budget(self, timeout, exc_class):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise exc_class("Request exceeded its total time budget.")
        return remaining if timeout is None else min(timeout, remaining)

    def read(self, max_bytes, timeout=None):
        return self._stream.read(max_bytes, self._budget(timeout, httpcore.ReadTimeout))

    def write(self, buffer, timeout=None):
        self._stream.write(buffer, self._budget(timeout, httpcore.WriteTimeout))

    def close(self):
        self._stream.close()

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        budget = self._budget(timeout, httpcore.ConnectTimeout)
        return _DeadlineStream(
            self._stream.start_tls(ssl_context, server_hostname, budget), self._deadline
        )

    def get_extra_info(self, info):
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, deadline):
        self._backend = httpcore.SyncBackend()
        self._deadline = deadline

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise httpcore.ConnectTimeout("Request exceeded its total time budget.")
        budget = remaining if timeout is None else min(timeout, remaining)
        stream = self._backend.connect_tcp(host, port, budget, local_address, socket_options)
        return _DeadlineStream(stream, self._deadline)

    def connect_unix_socket(self, *args, **kwargs):  # pragma: no cover - never used
        raise NotImplementedError

    def sleep(self, seconds):  # pragma: no cover - only used by httpcore retries (off)
        time.sleep(seconds)


class _DeadlineTransport(httpx.HTTPTransport):
    def __init__(self, deadline):
        super().__init__(verify=_ssl_context(), trust_env=False)
        # httpx has no public hook for the network backend, so replace the connection pool
        # it built with an identical one on the deadline backend. httpx (and so this
        # attribute) is pinned in requirements.txt; test_ssrf.py covers the swap.
        self._pool = httpcore.ConnectionPool(
            ssl_context=_ssl_context(), network_backend=_DeadlineBackend(deadline)
        )


def send(method, url, *, timeout, json=None, headers=None):
    """Validate `url`, then make one request to the validated IP. The body is never read.

    `timeout` is the TOTAL budget in seconds for DNS + connect + TLS + request + response
    headers. Raises BlockedTarget, DNSFailure, or an httpx.HTTPError for network failures
    (httpx.TimeoutException when the budget runs out).
    """
    deadline = time.monotonic() + timeout
    target = validate_target(url, deadline=deadline)
    address = _pick_address(target.addresses)
    pinned_url = target.url.copy_with(host=address)
    request_headers = {
        "Host": target.url.netloc.decode("ascii"),
        "User-Agent": settings.CHECK_USER_AGENT,
        **(headers or {}),
    }
    extensions = {"sni_hostname": target.host} if target.url.scheme == "https" else {}
    started = time.perf_counter()
    with (
        httpx.Client(
            transport=_DeadlineTransport(deadline),
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client,
        client.stream(
            method, pinned_url, headers=request_headers, json=json, extensions=extensions
        ) as response,
    ):
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        return Result(response.status_code, response.headers, elapsed_ms)
