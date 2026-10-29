"""
Los usuarios por nivel de acceso: superadmin de Ecofilia, miembros de cada
institución y público sin institución.

    python manage.py listar_usuarios_por_nivel

De cada uno: email, último login y cuántas operaciones tiene (como dueño).

Superadmin es ``apps.user.access.is_ecofilia_admin``: superusuario o
``role=admin``, sin institución. Un ``is_staff`` sin institución que no es
ninguna de las dos cosas cae en público, pero se marca: ``is_staff`` abre la
plataforma entera en varios chequeos y conviene revisarlo.
"""
from __future__ import annotations

from collections import defaultdict

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db.models import Count

from apps.user.access import is_ecofilia_admin


class Command(BaseCommand):
    help = "Lista los usuarios por nivel: superadmin, institución (con su slug) y público."

    def handle(self, *args, **options):
        users = (
            get_user_model().objects.select_related("organization")
            .annotate(operations_count=Count("projects", distinct=True))
            .order_by("email")
        )
        superadmins, publicos = [], []
        por_institucion: dict[str, list] = defaultdict(list)
        for user in users:
            if is_ecofilia_admin(user):
                superadmins.append(user)
            elif user.organization_id:
                por_institucion[user.organization.slug].append(user)
            else:
                publicos.append(user)

        self._section("Superadmin", superadmins)
        for slug in sorted(por_institucion):
            self._section(f"Institución: {slug}", por_institucion[slug])
        self._section("Público (sin institución)", publicos)

    def _section(self, title: str, users: list):
        self.stdout.write(f"\n== {title} ({len(users)}) ==")
        if not users:
            self.stdout.write("  (ninguno)")
            return
        for user in users:
            last_login = user.last_login.strftime("%Y-%m-%d %H:%M") if user.last_login else "nunca"
            marks = []
            if user.is_staff and not is_ecofilia_admin(user):
                marks.append("is_staff")
            if not user.is_active:
                marks.append("inactivo")
            suffix = f"  [{', '.join(marks)}]" if marks else ""
            self.stdout.write(
                f"  {user.email:<40} último login: {last_login:<16}  "
                f"operaciones: {user.operations_count}{suffix}"
            )
