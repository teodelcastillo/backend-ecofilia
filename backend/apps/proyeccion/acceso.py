"""Quién entra al módulo de proyección, y a la de qué institución.

Sólo el superadmin de Ecofilia (``apps.user.access.is_ecofilia_admin``). Entra
a la proyección de cualquier institución, que nombra con
``?organization=<slug>``. Los miembros de una institución —también sus
administradores— y quien no tiene institución no entran: el módulo es una
propuesta y por ahora no es de las instituciones.

Los datos siguen guardados por institución, así que abrirlo más adelante es
cambiar esta regla y no mover datos.
"""
from __future__ import annotations

from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError

from apps.user.access import is_ecofilia_admin
from apps.user.models import Organization


def puede_entrar(user, organization=None) -> bool:
    return is_ecofilia_admin(user)


def organizacion_del_pedido(request) -> Organization:
    """La institución sobre la que actúa el pedido, o el error que corresponde."""
    if not puede_entrar(request.user):
        raise PermissionDenied("La proyección del pipeline es sólo para el equipo de Ecofilia.")
    slug = (request.query_params.get("organization") or request.data.get("organization") or "").strip()
    if not slug:
        raise ValidationError({"organization": ["Indicá la institución."]})
    try:
        return Organization.objects.get(slug=slug)
    except Organization.DoesNotExist as exc:
        raise NotFound("No existe esa institución.") from exc
