from .models import Membership


def get_membership(user, org_slug):
    """The user's membership in the org with this slug, or None (callers turn None into 404)."""
    return (
        Membership.objects.select_related("organization")
        .filter(user=user, organization__slug=org_slug)
        .first()
    )


def memberships_for_user(user):
    return Membership.objects.select_related("organization").filter(user=user)


def members_of(organization):
    return Membership.objects.select_related("user").filter(organization=organization)
