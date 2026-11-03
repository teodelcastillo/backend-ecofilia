from django.apps import AppConfig


class AuthenticationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.authentication"

    def ready(self):
        # Import signals for token invalidation side-effects
        from apps.authentication import signals  # noqa: F401

        # El admin de Django con MFA, bloqueo por intentos y sin miembros de
        # instituciones (ver ``admin_login.py``).
        from django.contrib import admin

        from apps.authentication.admin_login import AdminMFAAuthenticationForm

        admin.site.login_form = AdminMFAAuthenticationForm
        admin.site.login_template = "admin/login_mfa.html"

