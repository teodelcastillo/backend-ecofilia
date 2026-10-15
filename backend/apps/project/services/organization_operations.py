"""
Qué operaciones son de una organización.

Es un dato de la operación (``Project.organization``), fijado al crearla: la
organización del dueño o, si la arma alguien del equipo de Ecofilia, la del
portal desde el que la crea. Antes se deducía en cada consulta —dueño de la
organización o algún asistente de ella habilitado—, y eso mezclaba
instituciones en cuanto dos compartían un asistente. La migración
``project/0016`` completó las existentes con ese criterio.
"""
from __future__ import annotations

from django.db.models import QuerySet


def organization_operations(orgs) -> QuerySet:
    """Las operaciones de ``orgs``."""
    from apps.project.models import Project

    return Project.objects.filter(organization__in=orgs)


def organization_default_skills_for(project) -> list:
    """Los asistentes por defecto de la organización de ``project``.

    Se llama al crear una operación. El formulario del portal CAF manda una
    lista fija de asistentes (IET y enverdecimiento); sin esto, un workflow
    asignado después a CAF nunca llegaba a las operaciones nuevas.
    """
    if project.organization_id is None:
        return []
    return list(project.organization.default_project_skills.all())
