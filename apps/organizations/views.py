from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import generics, status
from rest_framework.response import Response

from apps.core.throttles import WriteThrottle

from . import selectors, services
from .models import Membership
from .permissions import OrgScopedMixin
from .serializers import (
    InviteSerializer,
    MemberSerializer,
    MyOrganizationSerializer,
    OrganizationCreateSerializer,
    OrganizationUpdateSerializer,
    RoleSerializer,
)


class OrganizationListCreateView(generics.GenericAPIView):
    """Orgs I belong to. Anyone signed in can create one and becomes its owner."""

    serializer_class = MyOrganizationSerializer
    throttle_classes = [WriteThrottle]

    def get_queryset(self):
        return selectors.memberships_for_user(self.request.user)

    @extend_schema(operation_id="orgs_list")
    def get(self, request):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(MyOrganizationSerializer(page, many=True).data)

    @extend_schema(request=OrganizationCreateSerializer, responses={201: MyOrganizationSerializer})
    def post(self, request):
        serializer = OrganizationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        org = services.create_organization(user=request.user, **serializer.validated_data)
        membership = selectors.get_membership(request.user, org.slug)
        return Response(MyOrganizationSerializer(membership).data, status=status.HTTP_201_CREATED)


class OrganizationDetailView(OrgScopedMixin, generics.GenericAPIView):
    serializer_class = MyOrganizationSerializer
    write_roles = (Membership.Role.OWNER,)
    throttle_classes = [WriteThrottle]

    def get(self, request, org_slug):
        return Response(MyOrganizationSerializer(self.membership).data)

    @extend_schema(request=OrganizationUpdateSerializer, responses=MyOrganizationSerializer)
    def patch(self, request, org_slug):
        serializer = OrganizationUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.update_organization(organization=self.organization, **serializer.validated_data)
        return Response(MyOrganizationSerializer(self.membership).data)


class MemberListCreateView(OrgScopedMixin, generics.GenericAPIView):
    serializer_class = MemberSerializer
    throttle_classes = [WriteThrottle]

    def get_queryset(self):
        return selectors.members_of(self.organization)

    @extend_schema(operation_id="orgs_members_list")
    def get(self, request, org_slug):
        page = self.paginate_queryset(self.get_queryset())
        return self.get_paginated_response(MemberSerializer(page, many=True).data)

    @extend_schema(request=InviteSerializer, responses={201: MemberSerializer})
    def post(self, request, org_slug):
        serializer = InviteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = services.invite_member(actor=self.membership, **serializer.validated_data)
        return Response(MemberSerializer(membership).data, status=status.HTTP_201_CREATED)


class MemberDetailView(OrgScopedMixin, generics.GenericAPIView):
    serializer_class = MemberSerializer
    throttle_classes = [WriteThrottle]

    def get_object(self):
        return get_object_or_404(selectors.members_of(self.organization), pk=self.kwargs["pk"])

    def get(self, request, org_slug, pk):
        return Response(MemberSerializer(self.get_object()).data)

    @extend_schema(request=RoleSerializer, responses=MemberSerializer)
    def patch(self, request, org_slug, pk):
        membership = self.get_object()
        serializer = RoleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        membership = services.change_role(
            actor=self.membership, membership=membership, role=serializer.validated_data["role"]
        )
        return Response(MemberSerializer(membership).data)

    def delete(self, request, org_slug, pk):
        services.remove_member(actor=self.membership, membership=self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)
