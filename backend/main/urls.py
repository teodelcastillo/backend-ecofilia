from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from main.health import health_check


urlpatterns = [
    path('admin/', admin.site.urls),
    path('health/', health_check, name='health-check'),
    path('api-auth/', include('rest_framework.urls')),
    path('api/document/', include('apps.document.api.urls')),
    path('api/chat/', include('apps.chat.api.urls')),
    path('api/projects/', include('apps.project.api.urls')),
    path('api/repositories/', include('apps.repository.api.urls')),
    path('api/skills/', include('apps.skill.api.urls')),
    path('api/organizations/', include('apps.user.api.urls')),
    path('api/admin/', include('apps.user.api.admin_urls')),
    path('api/evaluations/', include('apps.evaluation.api.urls')),
    path('api/evaluation-templates/', include('apps.evaluation.api.urls_templates')),
    path('api/auth/', include('apps.authentication.api.urls')),
]
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
if getattr(settings, "SERVE_DJANGO_STATIC", False):
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
