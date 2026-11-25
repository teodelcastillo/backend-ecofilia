"""
Copias de un asistente (skill, copilot o workflow): la definición, sus pasos y
sus parámetros.

No copia corridas, versiones de definición ni habilitaciones (operaciones,
repositorios, instituciones): la copia arranca su historia y nadie la ve
habilitada hasta que alguien la habilite.
"""
from __future__ import annotations

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from apps.skill.models import Skill, SkillScope

COPY_SUFFIX = " (copia)"

INSTITUTION_COPY_FORBIDDEN = (
    "Un asistente de una institución sólo lo duplica Ecofilia: una copia tuya "
    "se iría con vos si dejás la institución."
)
RESTRICTED_COPY_FORBIDDEN = "Tu institución no puede crear asistentes; pedíselo a Ecofilia."


def copy_skill(source: Skill, **overrides) -> Skill:
    """Una copia de ``source`` con sus pasos y parámetros; ``overrides`` pisa campos.

    Sin ``slug`` en ``overrides``, el slug sale del nombre (único).
    """
    steps = list(source.steps.all())
    parameters = list(source.parameters.all())

    clone = Skill.objects.get(pk=source.pk)
    clone.pk = None
    clone.id = None
    clone._state.adding = True
    clone.slug = ""
    clone.is_template = False
    clone.is_default_enabled = False
    for field, value in overrides.items():
        setattr(clone, field, value)

    with transaction.atomic():
        clone.save()
        for item in steps + parameters:
            item.pk = None
            item.id = None
            item._state.adding = True
            item.skill = clone
            item.save()
    return clone


def duplicate_skill(source: Skill, user, name: str | None = None) -> Skill:
    """La copia que pide ``user`` desde el editor.

    - El superadmin la deja en el mismo alcance e institución que el original:
      duplicar un workflow de CAF da otro workflow de CAF, sin habilitar.
    - Los demás obtienen una copia propia (alcance ``user``). De los asistentes
      de su institución no: lo de una institución no sale de ella.

    Quién ve el original lo verifica la vista antes de llegar acá.
    """
    from apps.user.access import is_ecofilia_admin

    name = (name or "").strip()[:255] or f"{source.name}{COPY_SUFFIX}"[:255]

    if is_ecofilia_admin(user):
        return copy_skill(
            source,
            name=name,
            scope=source.scope,
            organization=source.organization,
            owner=user if source.scope == SkillScope.USER else source.owner,
        )

    if getattr(user, "is_restricted", False):
        raise PermissionDenied(RESTRICTED_COPY_FORBIDDEN)
    if source.scope == SkillScope.ORGANIZATION:
        raise PermissionDenied(INSTITUTION_COPY_FORBIDDEN)
    return copy_skill(source, name=name, scope=SkillScope.USER, organization=None, owner=user)
