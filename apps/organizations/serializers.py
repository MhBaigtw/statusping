from rest_framework import serializers

from .models import Membership


class MyOrganizationSerializer(serializers.ModelSerializer):
    """An org as seen by one member: the org's fields plus that member's role."""

    slug = serializers.SlugField(source="organization.slug", read_only=True)
    name = serializers.CharField(source="organization.name", read_only=True)
    status_page_public = serializers.BooleanField(
        source="organization.status_page_public", read_only=True
    )
    created_at = serializers.DateTimeField(source="organization.created_at", read_only=True)

    class Meta:
        model = Membership
        fields = ["slug", "name", "role", "status_page_public", "created_at"]
        read_only_fields = fields


class OrganizationCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    slug = serializers.SlugField(max_length=50, required=False)

    def validate_slug(self, value):
        return value.lower()


class OrganizationUpdateSerializer(serializers.Serializer):
    """Owner-only. The slug is immutable (it is the public status page URL)."""

    name = serializers.CharField(max_length=100, required=False)
    status_page_public = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Provide name and/or status_page_public.")
        return attrs


class MemberSerializer(serializers.ModelSerializer):
    email = serializers.EmailField(source="user.email", read_only=True)
    user_id = serializers.IntegerField(source="user.id", read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "user_id", "email", "role", "created_at"]
        read_only_fields = fields


class InviteSerializer(serializers.Serializer):
    email = serializers.EmailField()
    role = serializers.ChoiceField(choices=Membership.Role.choices)


class RoleSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=Membership.Role.choices)
