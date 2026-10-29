from django.urls import path

from apps.user.api.admin_views import (
    AdminOrganizationDetailView,
    AdminOrganizationMembersView,
    AdminOrganizationsView,
    AdminOverviewView,
    AdminSkillScopeView,
    AdminUserDetailView,
    AdminUsersView,
)

urlpatterns = [
    path("overview/", AdminOverviewView.as_view(), name="admin-overview"),
    path("organizations/", AdminOrganizationsView.as_view(), name="admin-organizations"),
    path("organizations/<slug:slug>/", AdminOrganizationDetailView.as_view(), name="admin-organization"),
    path(
        "organizations/<slug:slug>/members/",
        AdminOrganizationMembersView.as_view(),
        name="admin-organization-members",
    ),
    path("users/", AdminUsersView.as_view(), name="admin-users"),
    path("users/<int:pk>/", AdminUserDetailView.as_view(), name="admin-user"),
    path("skills/scope/", AdminSkillScopeView.as_view(), name="admin-skills-scope"),
]
