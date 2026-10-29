from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.organizations.serializers import MyOrganizationSerializer

from .models import User, normalize_email


class MeSerializer(serializers.ModelSerializer):
    organizations = MyOrganizationSerializer(source="memberships", many=True, read_only=True)

    class Meta:
        model = User
        fields = ["id", "email", "first_name", "last_name", "organizations"]
        read_only_fields = fields


class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, trim_whitespace=False)


class RegisteredUserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email"]


class EmailTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Emails are stored lowercase, so normalise the login email the same way."""

    def validate(self, attrs):
        attrs[self.username_field] = normalize_email(attrs[self.username_field])
        return super().validate(attrs)
