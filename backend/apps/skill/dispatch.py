"""Encolar una corrida de agente sin perder el error si falla.

Mismo problema que tuvo la ingesta de documentos (``apps.document.dispatch``):
``run_skill_task.delay()`` habla con SQS, y si el encolado falla la corrida
queda en ``pending`` para siempre — con una persona mirando la pantalla, que no
distingue "en la fila" de "perdida". Acá el fallo queda escrito en la corrida,
y el momento del despacho queda sellado para que el reaper
(``apps.skill.reliability.requeue_pending_executions``) sepa cuánto hace que
espera.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.skill.models import ExecutionStatus, SkillExecution
from apps.skill.tasks import run_skill_task

logger = logging.getLogger(__name__)


def mark_dispatched(execution_id: int) -> None:
    execution = SkillExecution.objects.only("metadata").get(pk=execution_id)
    metadata = dict(execution.metadata or {})
    metadata["dispatched_at"] = timezone.now().isoformat()
    SkillExecution.objects.filter(pk=execution_id).update(metadata=metadata)


def dispatch_execution(execution_id: int) -> bool:
    """Encola la corrida. Devuelve False —y la deja FAILED— si no pudo."""
    mark_dispatched(execution_id)
    try:
        run_skill_task.delay(execution_id)
        return True
    except Exception as exc:  # broker caído, credenciales, throttling…
        logger.exception("No se pudo encolar la ejecución %s: %s", execution_id, exc)
        SkillExecution.objects.filter(pk=execution_id).update(
            status=ExecutionStatus.FAILED,
            finished_at=timezone.now(),
            error_message=(
                "No se pudo poner la corrida en la cola de trabajo "
                f"({exc}). No se ejecutó ningún paso: volvé a lanzarla."
            ),
        )
        return False
