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
