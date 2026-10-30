import pytest
from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle

from apps.accounts.models import User

from .conftest import assert_error
from .factories import PASSWORD, MembershipFactory, UserFactory

pytestmark = pytest.mark.django_db

STRONG = "correct-horse-battery-staple-9"


def test_register_creates_user_with_lowercase_email(anon):
    resp = anon.post(
        "/api/auth/register/", {"email": "  New.User@Example.COM ", "password": STRONG}
    )
    assert resp.status_code == 201, resp.content
    user = User.objects.get()
    assert resp.json() == {"id": user.id, "email": "new.user@example.com"}
    assert user.check_password(STRONG)
    assert "password" not in resp.json()


def test_register_rejects_duplicate_email_case_insensitively(anon):
    UserFactory(email="taken@example.com")
    resp = anon.post("/api/auth/register/", {"email": "TAKEN@example.com", "password": STRONG})
    error = assert_error(resp, 400, "validation_error")
    assert "email" in error["details"]
    assert User.objects.count() == 1


@pytest.mark.parametrize("password", ["short1", "password123", "12345678901", "newuser@example"])
def test_register_applies_django_password_validators(anon, password):
    resp = anon.post("/api/auth/register/", {"email": "newuser@example.com", "password": password})
    error = assert_error(resp, 400, "validation_error")
    assert "password" in error["details"]
    assert not User.objects.exists()


def test_register_requires_fields(anon):
    error = assert_error(anon.post("/api/auth/register/", {}), 400, "validation_error")
    assert set(error["details"]) == {"email", "password"}


def test_register_then_login_case_insensitive(anon):
    anon.post("/api/auth/register/", {"email": "mixed@example.com", "password": STRONG})
    resp = anon.post("/api/auth/token/", {"email": "  MIXED@Example.com", "password": STRONG})
    assert resp.status_code == 200, resp.content
    assert {"access", "refresh"} <= set(resp.json())


def test_token_obtain_and_refresh(anon):
    user = UserFactory()
    tokens = anon.post("/api/auth/token/", {"email": user.email, "password": PASSWORD}).json()
    resp = anon.post("/api/auth/token/refresh/", {"refresh": tokens["refresh"]})
    assert resp.status_code == 200
    anon.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access']}")
    assert anon.get("/api/me/").json()["email"] == user.email


def test_bad_credentials_use_error_shape(anon):
    user = UserFactory()
    resp = anon.post("/api/auth/token/", {"email": user.email, "password": "wrong"})
    assert_error(resp, 401, "no_active_account")


def test_invalid_tokens_use_error_shape(anon):
    assert_error(anon.post("/api/auth/token/refresh/", {"refresh": "x"}), 401, "token_not_valid")
    anon.credentials(HTTP_AUTHORIZATION="Bearer garbage")
    assert_error(anon.get("/api/me/"), 401, "token_not_valid")


def test_me_lists_my_orgs_and_roles(client_for):
    user = UserFactory()
    MembershipFactory(user=user, organization__slug="a", role="owner")
    MembershipFactory(user=user, organization__slug="b", role="viewer")
    MembershipFactory(organization__slug="not-mine")
    body = client_for(user).get("/api/me/").json()
    assert body["email"] == user.email
    assert [(o["slug"], o["role"]) for o in body["organizations"]] == [
        ("a", "owner"),
        ("b", "viewer"),
    ]


def test_me_requires_auth(anon):
    assert_error(anon.get("/api/me/"), 401, "not_authenticated")


def test_jwt_lifetimes_default_to_spec():
    assert settings.SIMPLE_JWT["ACCESS_TOKEN_LIFETIME"].total_seconds() == 15 * 60
    assert settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].days == 1


def test_user_manager_and_save_normalise_email():
    user = User.objects.create_user("Mixed@Example.COM", "pw")
    assert user.email == "mixed@example.com"
    assert not user.is_staff
    user.email = "UPPER@EXAMPLE.COM"
    user.save()
    user.refresh_from_db()
    assert user.email == "upper@example.com"
    with pytest.raises(ValueError):
        User.objects.create_user("", "pw")
    admin = User.objects.create_superuser("admin@example.com", "pw")
    assert admin.is_staff and admin.is_superuser
    assert str(admin) == "admin@example.com"


# Throttling


@pytest.fixture
def low_rates(monkeypatch):
    # DRF reads rates into a class attribute at import time; patch that, not settings.
    monkeypatch.setattr(
        SimpleRateThrottle,
        "THROTTLE_RATES",
        {"login": "3/min", "token_refresh": "2/min", "register": "2/min", "writes": "2/min"},
    )


def assert_throttled(resp):
    error = assert_error(resp, 429, "throttled")
    assert int(resp["Retry-After"]) > 0
    assert error["details"]["retry_after_seconds"] == int(resp["Retry-After"])


def test_default_rates_and_shared_cache():
    from config.settings import base

    assert base.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] == {
        "login": "10/min",
        "token_refresh": "10/min",
        "register": "5/hour",
        "writes": "60/min",
        "status_page": "120/min",
    }
    assert base.CACHES["default"]["BACKEND"] == "django.core.cache.backends.db.DatabaseCache"


def test_login_is_throttled_per_ip(anon, low_rates):
    user = UserFactory()
    for _ in range(3):
        resp = anon.post("/api/auth/token/", {"email": user.email, "password": "wrong"})
        assert resp.status_code == 401
    # Even the right password is refused once this IP is over the limit.
    assert_throttled(anon.post("/api/auth/token/", {"email": user.email, "password": PASSWORD}))
    other_ip = anon.post(
        "/api/auth/token/", {"email": user.email, "password": PASSWORD}, REMOTE_ADDR="10.0.0.2"
    )
    assert other_ip.status_code == 200


def test_register_is_throttled_per_ip(anon, low_rates):
    for n in range(2):
        resp = anon.post("/api/auth/register/", {"email": f"u{n}@example.com", "password": STRONG})
        assert resp.status_code == 201
    assert_throttled(
        anon.post("/api/auth/register/", {"email": "u9@example.com", "password": STRONG})
    )
    assert User.objects.count() == 2


def test_refresh_is_throttled(anon, low_rates):
    for _ in range(2):
        anon.post("/api/auth/token/refresh/", {"refresh": "x"})
    assert_throttled(anon.post("/api/auth/token/refresh/", {"refresh": "x"}))


def test_x_forwarded_for_is_ignored_without_proxies(anon, low_rates):
    user = UserFactory()
    for n in range(3):
        anon.post(
            "/api/auth/token/",
            {"email": user.email, "password": "wrong"},
            HTTP_X_FORWARDED_FOR=f"203.0.113.{n}",
        )
    assert_throttled(
        anon.post(
            "/api/auth/token/",
            {"email": user.email, "password": "wrong"},
            HTTP_X_FORWARDED_FOR="203.0.113.99",
        )
    )


def test_writes_are_throttled_per_user_and_reads_are_not(client_for, low_rates):
    user = UserFactory()
    client = client_for(user)
    for n in range(2):
        assert client.post("/api/orgs/", {"name": f"Org {n}"}).status_code == 201
    assert_throttled(client.post("/api/orgs/", {"name": "Org 3"}))
    for _ in range(5):
        assert client.get("/api/orgs/").status_code == 200
