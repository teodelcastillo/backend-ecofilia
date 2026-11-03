"""
Qué asistentes ve cada usuario. Ninguna institución ve los de otra.

Hasta la fase 0 la regla era ``owner__isnull=True | owner=user``: todo lo que no
tenía dueño lo veía cualquiera, incluidos los workflows armados para una sola
institución. Ahora decide ``Skill.scope``:

- el superadmin de Ecofilia (``is_ecofilia_admin``) ve todos;
- un miembro de una institución ve los públicos, los de su institución y los
  suyos. Si la institución es ``restricted``, de los de su institución sólo los
  que Ecofilia le habilitó (``enabled_for_organizations``);
- un usuario sin institución ve los públicos y los suyos.

Los de plataforma no los ve nadie más que el superadmin.

Es la única definición de esta regla: el listado, el detalle y todas las
acciones que corren, reanudan o rehacen una corrida pasan por acá.
"""
from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.skill.models import Skill, SkillScope


def visible_skills_q(user) -> Q | None:
    """Condición de filtro de ``Skill`` para ``user``. ``None`` es «todos»."""
    from apps.user.access import is_ecofilia_admin

    if is_ecofilia_admin(user):
        return None
    if not getattr(user, "is_authenticated", False):
        return Q(pk__in=[])

    visible = Q(scope=SkillScope.PUBLIC) | Q(scope=SkillScope.USER, owner_id=user.pk)
    organization_id = getattr(user, "organization_id", None)
    if organization_id:
        own = Q(scope=SkillScope.ORGANIZATION, organization_id=organization_id)
        if getattr(user, "is_restricted", False):
            # Subconsulta y no JOIN: un JOIN a ``enabled_for_organizations``
            # duplicaría filas y obligaría a ``distinct()`` a todo el listado.
            own &= Q(
                pk__in=Skill.enabled_for_organizations.through.objects.filter(
                    organization_id=organization_id
                ).values("skill_id")
            )
        visible |= own
    return visible


def visible_skills(user, queryset: QuerySet[Skill] | None = None) -> QuerySet[Skill]:
    qs = Skill.objects.all() if queryset is None else queryset
    condition = visible_skills_q(user)
    return qs if condition is None else qs.filter(condition)


def user_can_see_skill(user, skill: Skill | None) -> bool:
    if skill is None:
        return False
    return visible_skills(user, Skill.objects.filter(pk=skill.pk)).exists()
