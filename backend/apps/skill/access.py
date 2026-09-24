from __future__ import annotations

from django.db.models import Q, QuerySet

from apps.skill.models import SkillExecution


def executions_queryset_for_user(user) -> QuerySet[SkillExecution]:
    """Executions owned by the user or tied to a project/repository they can access."""
    if user.is_staff:
        return SkillExecution.objects.all()

    from apps.project.models import Project
    from apps.repository.models import Repository

    project_ids = Project.objects.for_user(user).values_list("pk", flat=True)
    repo_ids = Repository.objects.for_user(user).values_list("pk", flat=True)
    return SkillExecution.objects.filter(
        Q(owner=user)
        | Q(project_id__in=project_ids)
        | Q(repository_id__in=repo_ids)
    ).distinct()


def user_can_view_execution(user, execution: SkillExecution) -> bool:
    if user.is_staff or execution.owner_id == user.id:
        return True
    if execution.project_id and execution.project.can_view(user):
        return True
    if execution.repository_id and execution.repository.can_view(user):
        return True
    return False


def user_can_mutate_execution(user, execution: SkillExecution) -> bool:
    return user.is_staff or execution.owner_id == user.id


def user_can_edit_execution_report(user, execution: SkillExecution) -> bool:
    """
    Quién puede trabajar el informe que salió de una corrida.

    Es más amplio que `user_can_mutate_execution` a propósito: relanzar o
    borrar una corrida es de quien la lanzó, pero redactar el informe es de
    quien tiene la operación a cargo. El ejecutivo que edita el IET
    normalmente no es el que apretó "ejecutar agente", y pedirle ser dueño de
    la corrida lo dejaría afuera de su propio informe.
    """
    if user_can_mutate_execution(user, execution):
        return True
    return bool(execution.project_id and execution.project.can_edit(user))


def user_can_run_assistants(user) -> bool:
    """
    Quién puede ejecutar asistentes (skills y workflows): lanzarlos, reanudarlos,
    repetirlos o avanzar un paso.

    Sólo superadmins —superusuarios de Ecofilia y administradores—, la misma
    regla que usa el frontend (``isCafSuperAdmin``). El resto ve las
    operaciones, sus informes y el chat, pero no dispara corridas: cada una
    cuesta varios dólares y cambia el informe de la operación.
    """
    from apps.user.models import UserRole

    return bool(
        getattr(user, "is_authenticated", False)
        and (user.is_superuser or getattr(user, "role", None) == UserRole.ADMIN)
    )


RUN_FORBIDDEN_MESSAGE = (
    "Ejecutar asistentes está habilitado sólo para administradores de Ecofilia."
)
