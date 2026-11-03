from .base import *

ENVIRONMENT_NAME = "development"

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'



# INSTALLED_APPS += [
#     'debug_toolbar',
# ]

# MIDDLEWARE += [
#     'debug_toolbar.middleware.DebugToolbarMiddleware',
# ]

ALLOWED_HOSTS += ["localhost"]

# En local, además del JWT, la sesión y la API navegable de DRF. Nunca en
# producción (ver ``base.py``).
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.authentication.org_scope.OrgScopedJWTAuthentication",
        "apps.authentication.org_scope.OrgScopedSessionAuthentication",
    ),
}
INTERNAL_IPS = [
    "localhost",
]


def show_toolbar(request):
    return True


DEBUG_TOOLBAR_CONFIG = {
    "SHOW_TOOLBAR_CALLBACK": show_toolbar,
}
