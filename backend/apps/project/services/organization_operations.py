"""
Qué operaciones son de una organización, para los asistentes asignados a ella.

No alcanza con mirar quién la creó. Muchas operaciones CAF las arma alguien
de staff de Ecofilia, que no pertenece a CAF: con el criterio «dueño de la
organización» quedaban afuera, y un workflow asignado a CAF no aparecía en
ellas. Una operación es de una organización si la creó alguien de esa
organización **o** si ya tiene habilitado algún asistente de esa
organización —el IET, en el caso de CAF—. Es el mismo criterio que usa el
portal para decidir qué es una operación CAF.
"""
from __future__ import annotations

from django.db.models import Q, QuerySet


def organization_operations(orgs, *, exclude_skill=None) -> QuerySet:
    """Las operaciones de ``orgs``: de sus miembros, o con alguno de sus asistentes.

    ``exclude_skill`` saca a ese asistente del segundo criterio: al asignarlo,
    que ya esté habilitado en una operación no la vuelve de la organización.
    """
    from apps.project.models import Project
    from apps.skill.models import Skill

    org_skills = Skill.objects.filter(enabled_for_organizations__in=orgs)
    if exclude_skill is not None:
        org_skills = org_skills.exclude(pk=exclude_skill.pk)
    return Project.objects.filter(
        Q(owner__organization__in=orgs) | Q(enabled_skills__in=org_skills)
    ).distinct()


def organization_default_skills_for(project) -> list:
    """Los asistentes por defecto de las organizaciones a las que pertenece ``project``.

    Se llama al crear una operación. El formulario del portal CAF manda una
    lista fija de asistentes (IET y enverdecimiento); sin esto, un workflow
    asignado después a CAF nunca llegaba a las operaciones nuevas.
    """
    from apps.skill.models import Skill
    from apps.user.models import Organization

    orgs = Organization.objects.filter(
        Q(members=project.owner) | Q(enabled_skills__in=project.enabled_skills.all())
    ).distinct()
    return list(Skill.objects.filter(default_for_organizations__in=orgs).distinct())
