from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="project.fill_from_blueprint")
def fill_from_blueprint_task(project_id: int) -> list[str]:
    """Objetivo y componentes de la operación, apenas su documento principal
    se puede leer (ver ``fill_missing_from_blueprint``)."""
    from apps.project.services.ai_fill import fill_missing_from_blueprint

    try:
        return sorted(fill_missing_from_blueprint(project_id))
    except ValueError as exc:
        # Sin texto legible o sin documento principal: no hay nada que
        # reintentar. Queda el botón "Generar" del Resumen.
        logger.info("ai_fill automático omitido para la operación %s: %s", project_id, exc)
        return []
    finally:
        # Con el principal ya leído (y objetivo y componentes completos si se
        # pudo), lo que la etapa dejó bloqueado puede arrancar.
        _retry_stage_launches(project_id)


def _retry_stage_launches(project_id: int) -> None:
    try:
        from apps.project.models import Project
        from apps.project.services.stages import retry_pending_stage_launches

        project = Project.objects.filter(pk=project_id).first()
        if project is not None:
            retry_pending_stage_launches(project)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudieron reintentar los lanzamientos de la operación %s", project_id)


def dispatch_fill_for_project(project_id: int) -> None:
    """Encola el autocompletado (y el reintento de la etapa) de una operación.

    Nunca levanta: se llama después de guardar la operación.
    """
    try:
        fill_from_blueprint_task.delay(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo encolar el autocompletado de la operación %s", project_id)


def dispatch_stage_followups(document_id: int) -> None:
    """Lo que habilita un documento que terminó de procesarse: la lectura del
    informe subido de un ejecutivo y los lanzamientos bloqueados de su etapa.

    Nunca levanta: se llama al final de la ingesta.
    """
    try:
        from apps.project.services.stage_reports import run_followups_for_document

        run_followups_for_document(document_id)
    except Exception:  # noqa: BLE001
        logger.exception("Falló el seguimiento de etapa del documento %s", document_id)


def dispatch_fill_for_blueprint(document_id: int) -> None:
    """Encola el autocompletado de toda operación cuyo principal es este documento.

    Nunca levanta: se llama al final de la ingesta, y un problema acá no puede
    dejar al documento como fallido.
    """
    try:
        from apps.project.models import Project

        project_ids = list(
            Project.objects.filter(blueprint_document_id=document_id).values_list("id", flat=True)
        )
        for project_id in project_ids:
            fill_from_blueprint_task.delay(project_id)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo encolar el autocompletado para el documento %s", document_id)
