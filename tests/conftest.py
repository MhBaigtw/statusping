import socket

import pytest
import respx
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from apps.core import safe_http

from .factories import (
    DISCORD_IP,
    PUBLIC_IP,
    SLACK_IP,
    MembershipFactory,
    OrganizationFactory,
    UserFactory,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Throttle counters live in the cache: start every test from zero."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """No test ever does a real DNS lookup. Map hostnames to addresses in this dict.

    A host that isn't in the dict fails to resolve, the same as NXDOMAIN.
    """
    records = {
        "example.com": [PUBLIC_IP],
        "discord.com": [DISCORD_IP],
        "hooks.slack.com": [SLACK_IP],
    }

    def fake_getaddrinfo(host, port):
        if host not in records:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port),
            )
            for address in records[host]
        ]

    monkeypatch.setattr(safe_http, "_getaddrinfo", fake_getaddrinfo)
    return records


@pytest.fixture(autouse=True)
def http():
    """No test ever makes a real HTTP request: any request without a route raises."""
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def anon():
    return APIClient()


@pytest.fixture
def client_for():
    """Return an APIClient authenticated with a real JWT access token for `user`."""

    def make(user):
        client = APIClient()
        token = RefreshToken.for_user(user).access_token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    return make


@pytest.fixture
def team(db):
    """An org with one member per role, plus an outsider who owns a different org."""
    org = OrganizationFactory(slug="acme")
    other_org = OrganizationFactory(slug="globex")
    people = {}
    for role in ("owner", "admin", "viewer"):
        people[role] = UserFactory(email=f"{role}@acme.example")
        MembershipFactory(user=people[role], organization=org, role=role)
    people["outsider"] = UserFactory(email="outsider@globex.example")
    MembershipFactory(user=people["outsider"], organization=other_org, role="owner")
    return {"org": org, "other_org": other_org, **people}


@pytest.fixture
def as_role(team, client_for, anon):
    """as_role("owner") -> an API client for that person; "anon" -> unauthenticated."""

    def make(role):
        return anon if role == "anon" else client_for(team[role])

    return make


def assert_error(response, status_code, code=None):
    """Assert the standard error shape and return the error object."""
    assert response.status_code == status_code, response.content
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "details"}, error
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    if code is not None:
        assert error["code"] == code, error
    return error


# Expected status for each actor in the standard permission matrix.
READ_MATRIX = {"owner": 200, "admin": 200, "viewer": 200, "outsider": 404, "anon": 401}
WRITE_MATRIX = {"owner": 2, "admin": 2, "viewer": 403, "outsider": 404, "anon": 401}


def check_status(response, expected):
    """`expected` is an exact status (4xx also checks the error shape), or 2 for any 2xx."""
    if expected == 2:
        assert 200 <= response.status_code < 300, response.content
    elif expected >= 400:
        assert_error(response, expected)
    else:
        assert response.status_code == expected, response.content
