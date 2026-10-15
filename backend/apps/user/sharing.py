"""
Con quién se puede compartir un recurso (operación, documento, repositorio,
evaluación).

Dos reglas, comunes a los cuatro:

1. **La organización es un límite.** Quien pertenece a una organización (CAF)
   sólo comparte con gente de su misma organización, y quien no pertenece a
   ninguna no comparte con miembros de una. Antes cualquiera podía compartir
   con cualquier email de la plataforma: un ejecutivo CAF podía abrirle una
   operación a un usuario de otro cliente, y un usuario cualquiera podía
   meter proyectos en el portal de CAF. El staff de Ecofilia queda afuera de
   la regla en las dos puntas: arma operaciones para los clientes y ya ve
   todo.

2. **El error no dice si el email existe.** "No existe un usuario con el
   email X" permitía averiguar quién tiene cuenta en Ecofilia probando
   emails. Ahora un email inexistente, inactivo o de otra organización
   recibe la misma respuesta.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from rest_framework import serializers

SHARE_TARGET_UNAVAILABLE = (
    "No hay nadie con ese email con quien puedas compartir. "
    "Revisá que esté bien escrito y que la persona sea de tu organización."
)


def within_organization_boundary(a, b) -> bool:
    """Si ``a`` y ``b`` pueden compartir recursos entre sí (regla 1).

    Misma organización —o ninguna de las dos—, o alguno es del equipo de
    Ecofilia. ``None`` cuenta como Ecofilia: los repositorios públicos no
    tienen dueño.
    """
    if a is None or b is None:
        return True
    if _is_ecofilia_team(a) or _is_ecofilia_team(b):
        return True
    return getattr(a, "organization_id", None) == getattr(b, "organization_id", None)


def _is_ecofilia_team(user) -> bool:
    # Staff sin organización: un miembro de un cliente con ``is_staff`` es un
    # dato mal cargado, no alguien del equipo (ver ``apps/user/access.py``).
    return bool(getattr(user, "is_staff", False) and not getattr(user, "organization_id", None))


def belongs_to_resource_organization(target, organization_id) -> bool:
    """Si ``target`` puede tener acceso a un recurso de esa institución.

    Una operación de CAF sólo se comparte con gente de CAF (o del equipo de
    Ecofilia), la comparta quien la comparta: también si la comparte el staff.
    """
    if organization_id is None:
        return True
    return _is_ecofilia_team(target) or getattr(target, "organization_id", None) == organization_id


def can_share_with(sharer, target, organization_id=None) -> bool:
    """Si ``sharer`` puede compartir con ``target`` un recurso de esa institución."""
    return (
        target.is_active
        and within_organization_boundary(sharer, target)
        and belongs_to_resource_organization(target, organization_id)
    )


def resolve_share_target(sharer, email: str, organization_id=None):
    """El usuario con ese email, si ``sharer`` puede compartir con él un
    recurso de la institución ``organization_id`` (``None``: sin institución).

    Levanta ``ValidationError`` sobre ``user_email`` con el mismo mensaje en
    todos los casos de rechazo (regla 2).
    """
    User = get_user_model()
    target = User.objects.filter(email=email).first()
    if target is None or not can_share_with(sharer, target, organization_id):
        raise serializers.ValidationError({"user_email": SHARE_TARGET_UNAVAILABLE})
    return target
