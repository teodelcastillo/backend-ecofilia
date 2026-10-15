import os
from datetime import timedelta


def _env_bool(name: str, default: str = "False") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.0/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ["SECRET_KEY"]
JWT_SIGNING_KEY = os.environ.get("JWT_SIGNING_KEY", SECRET_KEY)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _env_bool("DEBUG", "False")
ENVIRONMENT_NAME = "base"

# Hosts / CSRF / CORS
# Por defecto usamos los hosts de producción que tenés hoy:
_raw_hosts = os.environ.get("ALLOWED_HOSTS", "api.ecofilia.site,localhost,127.0.0.1")
ALLOWED_HOSTS = ["*"] if _raw_hosts == "*" else _env_list("ALLOWED_HOSTS", _raw_hosts)

CSRF_TRUSTED_ORIGINS = _env_list(
    "CSRF_TRUSTED_ORIGINS",
    "https://ecofilia.site,https://api.ecofilia.site,http://ecofilia.site,http://api.ecofilia.site,http://localhost:3000,https://www.ecofilia.site,https://www.ecofilia.host",
)

WSGI_APPLICATION = "main.wsgi.application"
SITE_ID = 1
STATIC_ROOT = "/django-static/"

# Additional configuration settings
LOGIN_REDIRECT_URL = "/"
SOCIALACCOUNT_QUERY_EMAIL = True
ACCOUNT_LOGOUT_ON_GET = True
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_REQUIRED = True


# Application definition

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sites",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "corsheaders",
    "pgvector",
    "rest_framework",
    "django_filters",
    "rest_framework.authtoken",
    "rest_framework_simplejwt.token_blacklist",
    "django_otp",
    "django_otp.plugins.otp_totp",
]

LOCAL_APPS = [
    "main",
    "apps.user",
    "apps.document",
    "apps.chat",
    "apps.project",
    "apps.repository",
    "apps.skill",
    "apps.evaluation",
    "apps.authentication",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

AUTH_USER_MODEL = "user.User"

###############################################################################
#  MIDDLEWARE
###############################################################################

MIDDLEWARE = [
    "main.middleware.HealthCheckMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# CORS: por defecto usamos los orígenes de producción actuales,
# pero se pueden sobreescribir con la env CORS_ALLOWED_ORIGINS
_cors_origins = _env_list(
    "CORS_ALLOWED_ORIGINS",
    "https://ecofilia.site,https://ecofilia.host,https://ecofilia.vercel.app,http://localhost:3000,https://www.ecofilia.site,https://www.ecofilia.host",
)
if _cors_origins:
    CORS_ALLOWED_ORIGINS = _cors_origins
    CORS_ALLOW_ALL_ORIGINS = False
else:
    CORS_ALLOW_ALL_ORIGINS = True

CORS_ALLOW_CREDENTIALS = True

CORS_ALLOW_METHODS = [
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]

CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
]


###############################################################################
#  TEMPLATES CONFIG
###############################################################################


ROOT_URLCONF = "main.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


###############################################################################
#  DATABASE CONFIG
###############################################################################

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ["POSTGRES_NAME"],
        "USER": os.environ["POSTGRES_USER"],
        "PASSWORD": os.environ["POSTGRES_PASSWORD"],
        "HOST": os.environ["POSTGRES_HOST"],
        "PORT": os.environ["POSTGRES_PORT"],
        "OPTIONS": {
            "sslmode": os.environ.get("POSTGRES_SSLMODE", "prefer"),
        },
    }
}

###############################################################################
#  DRF CONFIG
###############################################################################

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
    # Las de siempre (JWT, sesión, básica), con el alcance de la organización
    # aplicado: un miembro de una organización nunca llega con acceso global
    # (ver ``apps/authentication/org_scope.py``).
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "apps.authentication.org_scope.OrgScopedJWTAuthentication",
        "apps.authentication.org_scope.OrgScopedSessionAuthentication",
        "apps.authentication.org_scope.OrgScopedBasicAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_SCHEMA_CLASS": "rest_framework.schemas.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.environ.get("DRF_THROTTLE_RATE_ANON", "30/min"),
        "user": os.environ.get("DRF_THROTTLE_RATE_USER", "120/min"),
        # Throttle específico para refresh token (más estricto por seguridad)
        "strict_refresh": os.environ.get("DRF_THROTTLE_RATE_REFRESH", "20/min"),
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=int(os.environ.get("JWT_ACCESS_TTL_MINUTES", "15"))
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=int(os.environ.get("JWT_REFRESH_TTL_DAYS", "7"))
    ),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "ALGORITHM": os.environ.get("JWT_ALGORITHM", "HS256"),
    "SIGNING_KEY": JWT_SIGNING_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "UPDATE_LAST_LOGIN": True,
}


# Password validation
# https://docs.djangoproject.com/en/3.0/ref/settings/#auth-password-validators


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/3.0/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_L10N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.0/howto/static-files/

STATIC_URL = "/static/"
SERVE_DJANGO_STATIC = _env_bool("SERVE_DJANGO_STATIC", "False")


AUTHENTICATION_BACKENDS = ("django.contrib.auth.backends.ModelBackend",)

DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "no-reply@example.com")
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "http://localhost:3000")
MFA_ISSUER_NAME = os.environ.get("MFA_ISSUER_NAME", "Ecofilia")

# Email backend — usa SES en prod, consola en dev si no hay credenciales
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend",
)
AWS_SES_REGION_NAME = os.environ.get("AWS_SES_REGION_NAME", "us-east-2")
AWS_SES_REGION_ENDPOINT = f"email.{AWS_SES_REGION_NAME}.amazonaws.com"
AWS_SES_FROM_EMAIL = DEFAULT_FROM_EMAIL
# Deshabilita la llamada a ses:GetSendQuota que django-ses hace para throttling.
# El task role solo necesita ses:SendEmail y ses:SendRawEmail.
AWS_SES_AUTO_THROTTLE = None

SECURE_SSL_REDIRECT = _env_bool("SECURE_SSL_REDIRECT", "False")
SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", "False")
CSRF_COOKIE_SECURE = _env_bool("CSRF_COOKIE_SECURE", "False")
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = os.environ.get("CSRF_COOKIE_SAMESITE", "Lax")
CSRF_USE_SESSIONS = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"


# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Logging
#
# Sin esto un 500 en producción no dejaba rastro: con DEBUG apagado, Django
# manda el traceback de ``django.request`` sólo a ``mail_admins`` (que no está
# configurado) y CloudWatch no ve más que la línea de acceso de gunicorn. El
# 1-oct-2026 hubo cuatro subidas de documento seguidas en 500 y no había forma
# de saber por qué.
#
# ``django.request`` va a la consola en ERROR: los 5xx con su traceback, sin
# el ruido de cada 400/404 (que Django registra en WARNING). El resto de los
# loggers de la app salen por la raíz desde WARNING, como ya salían; con
# LOG_LEVEL=INFO se ven también los ``logger.info``. Los workers de Celery
# configuran su propio logging y no cambian.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "plain",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.environ.get("LOG_LEVEL", "WARNING"),
    },
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
    },
}
