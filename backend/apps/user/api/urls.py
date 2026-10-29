from django.urls import path

from apps.user.api.views import MyOrganizationConfigView, OrganizationConfigView

urlpatterns = [
    path("me/config/", MyOrganizationConfigView.as_view(), name="organization-config-me"),
    path("<slug:slug>/config/", OrganizationConfigView.as_view(), name="organization-config"),
]
