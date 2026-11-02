"""
El login del admin de Django, con el mismo nivel de protección que la API.

El admin es la puerta con más poder de la plataforma (ve y cambia todo, de
todas las instituciones) y entraba con email y contraseña:
- sin MFA;
- sin bloqueo por intentos fallidos;
- también para un miembro de una institución con ``is_staff`` por error, ya
  que el admin no pasa por ``scope_to_organization``.
"""
from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from apps.authentication.services import login_guard
from apps.authentication.services.mfa import MFAService


class AdminMFAAuthenticationForm(AdminAuthenticationForm):
    otp = forms.CharField(
        label="Código MFA",
        required=False,
        max_length=10,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric"}),
    )

    def clean(self):
        email = self.cleaned_data.get("username")
        candidate = get_user_model().objects.filter(email__iexact=email).first() if email else None
        if candidate is not None and login_guard.is_locked(candidate):
            raise self.get_invalid_login_error()
        try:
            return super().clean()
        except ValidationError:
            if candidate is not None:
                login_guard.register_failure(candidate)
            raise

    def confirm_login_allowed(self, user):
        super().confirm_login_allowed(user)  # activo y staff
        if user.organization_id:
            raise ValidationError(
                "Un usuario de una institución no entra al admin: tendría acceso a todas.",
                code="organization_member",
            )
        if not user.mfa_enabled:
            if not settings.ADMIN_REQUIRE_MFA:
                login_guard.register_success(user)
                return
            raise ValidationError(
                "El admin exige MFA. Activalo desde tu perfil en la aplicación y volvé a entrar.",
                code="mfa_required",
            )
        # Quien activó el MFA lo usa siempre, se exija o no: si no, el admin
        # sería una puerta sin segundo factor para una cuenta que sí lo tiene.
        if not MFAService.verify(user, self.cleaned_data.get("otp")):
            login_guard.register_failure(user)
            raise ValidationError("Código MFA inválido.", code="mfa_invalid")
        login_guard.register_success(user)
