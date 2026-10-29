import factory

from apps.accounts.models import User
from apps.alerts.models import AlertChannel
from apps.monitors.models import Monitor
from apps.organizations.models import Membership, Organization

PASSWORD = "test-pass-123"
PUBLIC_IP = "93.184.216.34"
DISCORD_IP = "162.159.135.232"
SLACK_IP = "54.0.0.10"


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    password = factory.PostGenerationMethodCall("set_password", PASSWORD)

    @factory.post_generation
    def _save(obj, create, extracted, **kwargs):
        if create:
            obj.save()


class OrganizationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Organization

    name = factory.Sequence(lambda n: f"Org {n}")
    slug = factory.Sequence(lambda n: f"org-{n}")


class MembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Membership

    user = factory.SubFactory(UserFactory)
    organization = factory.SubFactory(OrganizationFactory)
    role = Membership.Role.VIEWER


class MonitorFactory(factory.django.DjangoModelFactory):
    """Created directly (no DNS validation); point fake_dns at the host to run checks."""

    class Meta:
        model = Monitor

    organization = factory.SubFactory(OrganizationFactory)
    name = factory.Sequence(lambda n: f"Monitor {n}")
    url = "https://example.com/health"


class AlertChannelFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AlertChannel

    organization = factory.SubFactory(OrganizationFactory)
    kind = AlertChannel.Kind.DISCORD
    name = factory.Sequence(lambda n: f"Channel {n}")
    webhook_url = factory.Sequence(
        lambda n: f"https://discord.com/api/webhooks/{n}/secret-token-{n}"
    )
