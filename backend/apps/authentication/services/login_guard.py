"""
Bloqueo de la cuenta tras intentos fallidos de login.

Cada contraseña o código MFA equivocado suma un intento. Al llegar a
``LOGIN_MAX_FAILURES`` la cuenta queda bloqueada ``LOGIN_LOCKOUT_MINUTES``; un
login correcto vuelve el contador a cero. El throttle de DRF limita por IP; esto
limita por cuenta, que es lo que frena una adivinanza repartida entre muchas IP.

Estado en la base y no en cache: no hay cache compartida entre los workers de
la API, y un contador por proceso no frena nada.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import F
from django.utils import timezone


def is_locked(user) -> bool:
    return bool(user.locked_until and user.locked_until > timezone.now())


def register_failure(user) -> bool:
    """Suma un intento fallido. Devuelve si la cuenta quedó bloqueada."""
    from django.contrib.auth import get_user_model

    User = get_user_model()
    User.objects.filter(pk=user.pk).update(failed_login_attempts=F("failed_login_attempts") + 1)
    attempts = User.objects.filter(pk=user.pk).values_list("failed_login_attempts", flat=True).first() or 0
    if attempts >= settings.LOGIN_MAX_FAILURES:
        until = timezone.now() + timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)
        User.objects.filter(pk=user.pk).update(locked_until=until, failed_login_attempts=0)
        user.locked_until = until
        return True
    return False


def register_success(user) -> None:
    from django.contrib.auth import get_user_model

    if user.failed_login_attempts or user.locked_until:
        get_user_model().objects.filter(pk=user.pk).update(failed_login_attempts=0, locked_until=None)
        user.failed_login_attempts, user.locked_until = 0, None
