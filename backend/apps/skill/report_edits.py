"""
Mesa de trabajo del informe: edición por sección de una corrida.

Tres estados conviven para cada paso de una corrida:

  1. lo que escribió el agente — `output_structured["steps"]`, intocable;
  2. el borrador de quien está trabajando — `ExecutionSectionEdit.draft`;
  3. lo publicado, que es lo único que el informe muestra — `.published`.

Guardar no cambia el informe; publicar sí, y lo hace con todas las secciones
a la vez. Esa es la garantía que pidió el portal: se puede escribir sin miedo
y el IET recién se mueve cuando alguien lo decide.
"""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from apps.skill.models import (
    ExecutionSectionEdit,
    ExecutionSectionEditVersion,
    SkillExecution,
)


def step_ids_of(execution: SkillExecution) -> set[int]:
    """Los pasos que esta corrida efectivamente escribió."""
    steps = (execution.output_structured or {}).get("steps") or []
    ids: set[int] = set()
    for step in steps:
        step_id = step.get("step_id") if isinstance(step, dict) else None
        if isinstance(step_id, int):
            ids.add(step_id)
    return ids


def save_section_draft(
    execution: SkillExecution,
    step_id: int,
    content: str,
    user,
) -> ExecutionSectionEdit:
    """
    Guarda el borrador de una sección. No toca lo publicado.

    Un borrador idéntico a lo publicado se guarda igual: el frontend lo lee
    como "sin cambios" comparando los dos campos, y así una edición que vuelve
    sobre sus pasos no deja una publicación pendiente que no cambia nada.
    """
    edit, _ = ExecutionSectionEdit.objects.get_or_create(
        execution=execution, step_id=step_id
    )
    edit.draft = content
    edit.updated_by = user
    edit.save(update_fields=["draft", "updated_by", "updated_at"])
    return edit


def discard_section_draft(
    execution: SkillExecution, step_id: int
) -> ExecutionSectionEdit | None:
    """
    Tira el borrador y deja la sección como estaba publicada.

    Si nunca se publicó nada, la fila queda vacía y el informe vuelve a
    mostrar la salida del agente. No se borra la fila porque su historial de
    publicaciones tiene que sobrevivir a un descarte.
    """
    edit = ExecutionSectionEdit.objects.filter(
        execution=execution, step_id=step_id
    ).first()
    if edit is None:
        return None
    edit.draft = ""
    edit.save(update_fields=["draft", "updated_at"])
    return edit


@transaction.atomic
def publish_section_edits(
    execution: SkillExecution, user
) -> list[ExecutionSectionEdit]:
    """
    Publica todos los borradores pendientes de una corrida, de una sola vez.

    Devuelve las secciones que efectivamente cambiaron. Publicar sin nada
    pendiente no es un error: devuelve una lista vacía y no escribe historial.
    """
    now = timezone.now()
    published: list[ExecutionSectionEdit] = []

    edits = (
        ExecutionSectionEdit.objects.select_for_update()
        .filter(execution=execution)
        .order_by("step_id")
    )
    for edit in edits:
        if not edit.has_unpublished_changes:
            continue
        last = edit.versions.order_by("-version_number").first()
        ExecutionSectionEditVersion.objects.create(
            edit=edit,
            version_number=(last.version_number + 1) if last else 1,
            content=edit.draft,
            created_by=user,
        )
        edit.published = edit.draft
        edit.published_at = now
        edit.published_by = user
        edit.save(update_fields=["published", "published_at", "published_by"])
        published.append(edit)

    return published


def restore_section_version(
    execution: SkillExecution, step_id: int, version_number: int
) -> ExecutionSectionEdit | None:
    """
    Trae una publicación vieja al borrador.

    No publica: deja el texto listo para revisar y que la publicación sea
    siempre un acto explícito, igual que cualquier otro cambio.
    """
    edit = ExecutionSectionEdit.objects.filter(
        execution=execution, step_id=step_id
    ).first()
    if edit is None:
        return None
    version = edit.versions.filter(version_number=version_number).first()
    if version is None:
        return None
    edit.draft = version.content
    edit.save(update_fields=["draft", "updated_at"])
    return edit


def promote_execution(execution: SkillExecution) -> SkillExecution:
    """
    Asciende una corrida a vigente.

    La vigente de una operación es la que tiene la fecha más nueva entre
    `finished_at` y `promoted_at`, así que esto la pone adelante sin borrarle
    la fecha real de ejecución — y una corrida nueva vuelve a ganarle sola.
    """
    execution.promoted_at = timezone.now()
    execution.save(update_fields=["promoted_at"])
    return execution
