"""Idempotent demo data. Demo users get the password from DEMO_PASSWORD (never printed).

Monitors are created directly, without the create-time DNS lookup, so seeding works
offline. Their URLs are fixed, safe public targets, and every check re-validates them
anyway (SSRF checks run before each request).
"""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import User
from apps.monitors.models import Monitor
from apps.organizations.models import Membership, Organization

DOMAIN = "statusping.example"
ORG_SLUG = "demo"

USERS = [("owner", Membership.Role.OWNER), ("viewer", Membership.Role.VIEWER)]

# (name, url, method, expected_status)
MONITORS = [
    ("Example homepage", "https://example.com/", "GET", 200),
    ("httpbin 200 (HEAD)", "https://httpbin.org/status/200", "HEAD", 200),
    ("httpbin always 500", "https://httpbin.org/status/500", "GET", 200),
]


class Command(BaseCommand):
    help = "Create (or refresh) a demo org, users and monitors. Safe to run repeatedly."

    def handle(self, *args, **options):
        password = os.environ.get("DEMO_PASSWORD")
        if not password:
            raise CommandError("Set DEMO_PASSWORD in the environment (.env) first.")
        with transaction.atomic():
            org, _ = Organization.objects.get_or_create(
                slug=ORG_SLUG, defaults={"name": "Demo Org"}
            )
            for local, role in USERS:
                user, _ = User.objects.get_or_create(email=f"{local}@{DOMAIN}")
                user.set_password(password)
                user.save()
                Membership.objects.update_or_create(
                    user=user, organization=org, defaults={"role": role}
                )
            for name, url, method, expected in MONITORS:
                Monitor.objects.get_or_create(
                    organization=org,
                    name=name,
                    defaults={"url": url, "method": method, "expected_status": expected},
                )
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo org '{ORG_SLUG}': users "
                + ", ".join(f"{local}@{DOMAIN}" for local, _ in USERS)
                + f"; {len(MONITORS)} monitors. Password: DEMO_PASSWORD from your environment."
            )
        )
