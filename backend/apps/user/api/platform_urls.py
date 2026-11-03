from django.urls import path

from apps.user.api.views import PlatformConfigView

urlpatterns = [
    path("config/public/", PlatformConfigView.as_view(), {"key": "public"}, name="platform-config-public"),
    path("config/template/", PlatformConfigView.as_view(), {"key": "template"}, name="platform-config-template"),
]
