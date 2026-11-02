"""
Re-cifra los secretos MFA con la clave principal (la primera de
``MFA_ENCRYPTION_KEYS``).

    python manage.py recifrar_secretos_mfa --dry-run
    python manage.py recifrar_secretos_mfa

Se corre después de definir ``MFA_ENCRYPTION_KEYS`` por primera vez (hoy los
secretos están cifrados con la clave derivada de ``SECRET_KEY``) o después de
agregar una clave nueva adelante para rotar. Es idempotente, y no cambia ningún
secreto: sólo con qué clave está cifrado. Ver
``apps/authentication/services/secret_box.py``.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.authentication.services.secret_box import decrypt, rotate


class Command(BaseCommand):
    help = "Re-cifra los secretos MFA con la clave principal de MFA_ENCRYPTION_KEYS."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Sólo cuenta; no guarda nada.")

    def handle(self, *args, dry_run=False, **options):
        User = get_user_model()
        changed = 0
        with transaction.atomic():
            for user in User.objects.exclude(mfa_secret="").only("pk", "mfa_secret").select_for_update():
                new = rotate(user.mfa_secret)
                if decrypt(new) != decrypt(user.mfa_secret):  # nunca debería pasar
                    raise RuntimeError(f"El re-cifrado cambió el secreto del usuario {user.pk}; no se guardó nada.")
                if new != user.mfa_secret:
                    changed += 1
                    if not dry_run:
                        User.objects.filter(pk=user.pk).update(mfa_secret=new)
            if dry_run:
                transaction.set_rollback(True)
        verb = "se re-cifrarían" if dry_run else "re-cifrados"
        self.stdout.write(self.style.SUCCESS(f"Secretos MFA {verb}: {changed}."))
