"""Org scoping and role checks for every view under /api/orgs/{org_slug}/.

Non-members get 404, so an org's existence is never leaked. Members whose role does not
allow the request get 403. Safe methods need any role; everything else needs one of
`view.write_roles`, which is owner or admin unless a view narrows it.
"""

from rest_framework.exceptions import NotFound
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import ALL_ROLES, MANAGER_ROLES
from .selectors import get_membership


class OrgRolePermission(BasePermission):
    message = "Your role in this organization does not allow this."

    def has_permission(self, request, view):
        if not request.user.is_authenticated:
            return False  # DRF answers 401
        membership = get_membership(request.user, view.kwargs["org_slug"])
        if membership is None:
            raise NotFound()
        view.membership = membership
        view.organization = membership.organization
        roles = ALL_ROLES if request.method in SAFE_METHODS else view.write_roles
        return membership.role in roles


class OrgScopedMixin:
    """For views nested under orgs/{org_slug}/. Sets self.organization and self.membership."""

    permission_classes = [OrgRolePermission]
    write_roles = MANAGER_ROLES
    organization = None
    membership = None
