from django.conf import settings
from django.db import models


class Organization(models.Model):
    name = models.CharField(max_length=100)
    # Immutable once created: it becomes the public status page URL in Phase 3.
    slug = models.SlugField(max_length=50, unique=True)
    # Owners can hide the public status page (/status/{slug}/ then returns 404).
    status_page_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.slug


class Membership(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        VIEWER = "viewer", "Viewer"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="memberships"
    )
    role = models.CharField(max_length=10, choices=Role.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["user", "organization"], name="unique_membership"),
        ]

    def __str__(self):
        return f"{self.user} in {self.organization} ({self.role})"


# Owners and admins manage monitors, alert channels and members; viewers only read.
MANAGER_ROLES = (Membership.Role.OWNER, Membership.Role.ADMIN)
ALL_ROLES = tuple(Membership.Role)
