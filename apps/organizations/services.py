from django.db import IntegrityError, transaction
from django.utils.text import slugify
from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.accounts.models import User, normalize_email
from apps.core.exceptions import Conflict

from .models import Membership, Organization

Role = Membership.Role
SLUG_TAKEN = "This slug is already taken. Choose another one."


def create_organization(*, user, name, slug=None):
    """Create an org and make `user` its owner."""
    slug = (slug or slugify(name))[:50].strip("-")
    if not slug:
        raise ValidationError({"slug": ["Could not derive a slug from the name; provide one."]})
    if Organization.objects.filter(slug=slug).exists():
        raise ValidationError({"slug": [SLUG_TAKEN]})
    try:
        with transaction.atomic():
            org = Organization.objects.create(name=name, slug=slug)
            Membership.objects.create(user=user, organization=org, role=Role.OWNER)
    except IntegrityError as exc:  # lost a race for the slug
        raise ValidationError({"slug": [SLUG_TAKEN]}) from exc
    return org


def update_organization(*, organization, **fields):
    """Owner-only: name and/or status_page_public (the slug never changes)."""
    from apps.statuspage.services import invalidate

    for name, value in fields.items():
        setattr(organization, name, value)
    organization.save(update_fields=list(fields))
    invalidate(organization)
    return organization


def _require_owner_for_owner_changes(actor, *roles):
    if Role.OWNER in roles and actor.role != Role.OWNER:
        raise PermissionDenied("Only owners can grant, change or remove the owner role.")


def _lock_org(organization_id):
    """Serialise membership changes per org so the last-owner check cannot race."""
    Organization.objects.select_for_update().get(pk=organization_id)


def _ensure_not_last_owner(membership):
    if membership.role != Role.OWNER:
        return
    owners = Membership.objects.filter(organization_id=membership.organization_id, role=Role.OWNER)
    if owners.count() <= 1:
        raise Conflict("An organization must keep at least one owner.", code="last_owner")


def invite_member(*, actor, email, role):
    """Add an existing user (looked up by email) to the actor's organization."""
    _require_owner_for_owner_changes(actor, role)
    user = User.objects.filter(email=normalize_email(email)).first()
    if user is None:
        raise ValidationError({"email": ["No user with this email. They must register first."]})
    with transaction.atomic():
        _lock_org(actor.organization_id)
        if Membership.objects.filter(user=user, organization_id=actor.organization_id).exists():
            raise Conflict("This user is already a member.", code="already_member")
        return Membership.objects.create(
            user=user, organization_id=actor.organization_id, role=role
        )


def change_role(*, actor, membership, role):
    _require_owner_for_owner_changes(actor, membership.role, role)
    with transaction.atomic():
        _lock_org(membership.organization_id)
        membership.refresh_from_db()
        if membership.role == role:
            return membership
        _ensure_not_last_owner(membership)
        membership.role = role
        membership.save(update_fields=["role"])
    return membership


def remove_member(*, actor, membership):
    _require_owner_for_owner_changes(actor, membership.role)
    with transaction.atomic():
        _lock_org(membership.organization_id)
        membership.refresh_from_db()
        _ensure_not_last_owner(membership)
        membership.delete()
