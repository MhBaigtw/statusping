import pytest

from apps.organizations.models import Membership, Organization

from .conftest import READ_MATRIX, assert_error, check_status
from .factories import MembershipFactory, UserFactory

pytestmark = pytest.mark.django_db

ACTORS = ["owner", "admin", "viewer", "outsider", "anon"]


# Organizations


def test_create_org_makes_creator_owner(client_for):
    user = UserFactory()
    resp = client_for(user).post("/api/orgs/", {"name": "Acme Corp"})
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["slug"] == "acme-corp"
    assert body["role"] == "owner"
    org = Organization.objects.get(slug="acme-corp")
    assert Membership.objects.get(organization=org).user == user


def test_create_org_with_explicit_slug_and_duplicate_slug(client_for):
    client = client_for(UserFactory())
    assert client.post("/api/orgs/", {"name": "A", "slug": "Mine"}).json()["slug"] == "mine"
    error = assert_error(client.post("/api/orgs/", {"name": "B", "slug": "mine"}), 400)
    assert "slug" in error["details"]
    error = assert_error(client.post("/api/orgs/", {"name": "Mine"}), 400)
    assert "slug" in error["details"]


def test_create_org_rejects_unsluggable_name_and_bad_slug(client_for):
    client = client_for(UserFactory())
    assert "slug" in assert_error(client.post("/api/orgs/", {"name": "!!!"}), 400)["details"]
    resp = client.post("/api/orgs/", {"name": "X", "slug": "has spaces"})
    assert "slug" in assert_error(resp, 400)["details"]


def test_create_org_requires_auth(anon):
    assert_error(anon.post("/api/orgs/", {"name": "X"}), 401)


def test_list_shows_only_my_orgs(team, client_for):
    body = client_for(team["viewer"]).get("/api/orgs/").json()
    assert [(o["slug"], o["role"]) for o in body["results"]] == [("acme", "viewer")]


@pytest.mark.parametrize("actor", ACTORS)
def test_org_detail_matrix(as_role, actor):
    resp = as_role(actor).get("/api/orgs/acme/")
    check_status(resp, READ_MATRIX[actor])
    if resp.status_code == 200:
        assert resp.json()["slug"] == "acme"
        assert resp.json()["role"] == actor


def test_unknown_org_is_404_like_someone_elses(as_role):
    assert_error(as_role("owner").get("/api/orgs/no-such-org/"), 404, "not_found")
    assert_error(as_role("owner").get("/api/orgs/globex/"), 404, "not_found")


@pytest.mark.parametrize(
    "actor,expected",
    [("owner", 200), ("admin", 403), ("viewer", 403), ("outsider", 404), ("anon", 401)],
)
def test_only_owner_renames(team, as_role, actor, expected):
    resp = as_role(actor).patch("/api/orgs/acme/", {"name": "Acme 2"}, format="json")
    check_status(resp, expected)
    team["org"].refresh_from_db()
    assert (team["org"].name == "Acme 2") == (expected == 200)


def test_rename_does_not_change_slug(team, as_role):
    resp = as_role("owner").patch("/api/orgs/acme/", {"name": "New", "slug": "x"}, format="json")
    assert resp.status_code == 200
    assert resp.json()["slug"] == "acme"
    assert_error(as_role("owner").patch("/api/orgs/acme/", {}, format="json"), 400)


def test_put_is_not_allowed(as_role):
    assert_error(as_role("owner").put("/api/orgs/acme/", {"name": "X"}, format="json"), 405)


# Members


@pytest.mark.parametrize("actor", ACTORS)
def test_member_list_matrix(as_role, actor):
    resp = as_role(actor).get("/api/orgs/acme/members/")
    check_status(resp, READ_MATRIX[actor])
    if resp.status_code == 200:
        emails = {m["email"] for m in resp.json()["results"]}
        assert emails == {"owner@acme.example", "admin@acme.example", "viewer@acme.example"}


@pytest.mark.parametrize(
    "actor,expected",
    [("owner", 201), ("admin", 201), ("viewer", 403), ("outsider", 404), ("anon", 401)],
)
def test_invite_matrix(team, as_role, actor, expected):
    new = UserFactory(email="new@example.com")
    resp = as_role(actor).post(
        "/api/orgs/acme/members/", {"email": "NEW@example.com", "role": "viewer"}
    )
    check_status(resp, expected)
    joined = Membership.objects.filter(user=new, organization=team["org"]).exists()
    assert joined == (expected == 201)


def test_invite_unknown_email_and_existing_member(as_role):
    owner = as_role("owner")
    resp = owner.post("/api/orgs/acme/members/", {"email": "ghost@example.com", "role": "viewer"})
    assert "email" in assert_error(resp, 400, "validation_error")["details"]
    resp = owner.post("/api/orgs/acme/members/", {"email": "admin@acme.example", "role": "viewer"})
    assert_error(resp, 409, "already_member")
    resp = owner.post("/api/orgs/acme/members/", {"email": "admin@acme.example", "role": "boss"})
    assert_error(resp, 400, "validation_error")


def test_admin_cannot_grant_owner_but_owner_can(team, as_role):
    UserFactory(email="new@example.com")
    payload = {"email": "new@example.com", "role": "owner"}
    assert_error(as_role("admin").post("/api/orgs/acme/members/", payload), 403)
    assert as_role("owner").post("/api/orgs/acme/members/", payload).status_code == 201


def member_id(team, role):
    return Membership.objects.get(organization=team["org"], user=team[role]).pk


@pytest.mark.parametrize(
    "actor,expected",
    [("owner", 200), ("admin", 200), ("viewer", 403), ("outsider", 404), ("anon", 401)],
)
def test_change_role_matrix(team, as_role, actor, expected):
    url = f"/api/orgs/acme/members/{member_id(team, 'viewer')}/"
    resp = as_role(actor).patch(url, {"role": "admin"}, format="json")
    check_status(resp, expected)
    role = Membership.objects.get(pk=member_id(team, "viewer")).role
    assert role == ("admin" if expected == 200 else "viewer")


@pytest.mark.parametrize(
    "actor,expected",
    [("owner", 204), ("admin", 204), ("viewer", 403), ("outsider", 404), ("anon", 401)],
)
def test_remove_member_matrix(team, as_role, actor, expected):
    pk = member_id(team, "viewer")
    check_status(as_role(actor).delete(f"/api/orgs/acme/members/{pk}/"), expected)
    assert Membership.objects.filter(pk=pk).exists() == (expected != 204)


def test_member_detail_and_other_orgs_member_is_404(team, as_role):
    pk = member_id(team, "admin")
    assert as_role("viewer").get(f"/api/orgs/acme/members/{pk}/").json()["role"] == "admin"
    foreign = Membership.objects.get(user=team["outsider"]).pk
    assert_error(as_role("owner").get(f"/api/orgs/acme/members/{foreign}/"), 404)
    assert_error(as_role("owner").delete(f"/api/orgs/acme/members/{foreign}/"), 404)


def test_admin_cannot_demote_or_remove_an_owner(team, as_role):
    pk = member_id(team, "owner")
    resp = as_role("admin").patch(
        f"/api/orgs/acme/members/{pk}/", {"role": "viewer"}, format="json"
    )
    assert_error(resp, 403)
    assert_error(as_role("admin").delete(f"/api/orgs/acme/members/{pk}/"), 403)
    assert Membership.objects.get(pk=pk).role == "owner"


def test_last_owner_cannot_demote_or_remove_themself(team, as_role):
    pk = member_id(team, "owner")
    owner = as_role("owner")
    resp = owner.patch(f"/api/orgs/acme/members/{pk}/", {"role": "admin"}, format="json")
    assert_error(resp, 409, "last_owner")
    assert_error(owner.delete(f"/api/orgs/acme/members/{pk}/"), 409, "last_owner")
    assert Membership.objects.get(pk=pk).role == "owner"


def test_with_two_owners_one_may_step_down(team, as_role):
    second = UserFactory()
    MembershipFactory(user=second, organization=team["org"], role="owner")
    pk = member_id(team, "owner")
    resp = as_role("owner").patch(f"/api/orgs/acme/members/{pk}/", {"role": "admin"}, format="json")
    assert resp.status_code == 200
    # Now `second` is the only owner and is protected.
    second_pk = Membership.objects.get(user=second).pk
    assert_error(as_role("owner").delete(f"/api/orgs/acme/members/{second_pk}/"), 403)


def test_setting_the_same_role_is_a_no_op(team, as_role):
    pk = member_id(team, "owner")
    resp = as_role("owner").patch(f"/api/orgs/acme/members/{pk}/", {"role": "owner"}, format="json")
    assert resp.status_code == 200


def test_owner_can_remove_another_owner_but_not_the_last(team, as_role):
    second = UserFactory()
    second_pk = MembershipFactory(user=second, organization=team["org"], role="owner").pk
    assert as_role("owner").delete(f"/api/orgs/acme/members/{second_pk}/").status_code == 204
    owners = Membership.objects.filter(organization=team["org"], role="owner")
    assert owners.count() == 1


def test_org_str_and_membership_str(team):
    assert str(team["org"]) == "acme"
    assert "owner" in str(Membership.objects.get(user=team["owner"]))
