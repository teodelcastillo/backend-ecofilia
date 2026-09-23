"""
Detectar una ejecución que murió sin avisar.

El incidente que motiva este módulo: un worker recibió la tarea, un proceso
hijo murió por SIGKILL a los seis minutos, y nada en el código se enteró.
`SkillExecution` no tenía ningún campo de última señal de vida, así que ni un
proceso automático ni una persona podían distinguir "sigue viva, tranquilo" de
"murió hace una hora" — la ejecución quedó mostrando `running` en la interfaz
durante más de sesenta minutos, y la única forma de arreglarlo fue un shell de
producción.

``last_progress_at`` es esa señal, y este módulo es lo que la vigila: una tarea
periódica que busca ejecuciones `running` sin progreso reciente y las marca
`stalled` — un estado nuevo, deliberadamente distinto de `failed`. `failed`
sigue significando "hubo un error de negocio real" (un JSON inválido en modo
estricto, por ejemplo); `stalled` significa "el proceso murió sin decir por
qué". Confundirlos borraría justo la distinción que un panel de confiabilidad
necesita mostrar.

No se reintenta automáticamente acá. Reanudar gasta una llamada al modelo por
paso en progreso y hay un endpoint explícito para eso
(``SkillExecutionViewSet.resume``) — este módulo sólo diagnostica.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.skill.models import ExecutionStatus, SkillExecution

logger = logging.getLogger(__name__)

# La corrida más pesada medida hasta ahora completó un paso típico en unos
# pocos minutos incluso bajo el corpus más grande que tenemos (INFOTEP,
# 2.6M caracteres). Quince minutos sin que se persista un paso nuevo es ya
# varias veces ese tiempo — no es una corrida lenta, es una corrida muerta.
STALL_THRESHOLD_MINUTES = int(os.environ.get("SKILL_STALL_THRESHOLD_MINUTES", "15"))


def reap_stalled_executions(*, threshold_minutes: int | None = None) -> list[int]:
    """Marca `stalled` toda ejecución `running` sin progreso reciente.

    El umbral se compara contra ``last_progress_at``, y si ese campo está vacío
    —ejecuciones que arrancaron antes de que existiera, o que nunca llegaron a
    persistir un primer paso— contra ``started_at``, y si tampoco hay eso,
    contra ``created_at``. Sin esa cadena de respaldo, una ejecución zombi de
    antes de este campo quedaría invisible para siempre en vez de detectarse en
    la primera pasada.

    Devuelve los ids marcados, para que quien la invoque pueda loguearlos.
    """
    minutes = (
        threshold_minutes if threshold_minutes is not None else STALL_THRESHOLD_MINUTES
    )
    cutoff = timezone.now() - timedelta(minutes=minutes)

    stale = (
        SkillExecution.objects
        .filter(status=ExecutionStatus.RUNNING)
        .annotate(
            last_signal=Coalesce("last_progress_at", "started_at", "created_at")
        )
        .filter(last_signal__lt=cutoff)
    )
    ids = list(stale.values_list("id", flat=True))
    if ids:
        stale.update(
            status=ExecutionStatus.STALLED,
            error_message=(
                f"Sin progreso desde hace más de {minutes} minutos. El worker "
                "probablemente murió sin avisar — revisar los logs de ECS del "
                "worker interactivo alrededor de ese momento. Se puede "
                "reanudar desde el paso donde quedó, sin repetir los "
                "anteriores."
            ),
        )
        logger.warning("Ejecuciones marcadas stalled: %s", ids)
    return ids


# Una corrida en `pending` más de esto no está en la fila: su mensaje se perdió
# (encolado fallido, cola purgada, worker que lo tomó y murió antes de
# reclamarla). Con el worker interactivo libre, una corrida arranca en segundos.
PENDING_THRESHOLD_MINUTES = int(os.environ.get("SKILL_STUCK_PENDING_MINUTES", "10"))
MAX_AUTO_REQUEUES = int(os.environ.get("SKILL_MAX_AUTO_REQUEUES", "2"))


def requeue_pending_executions(*, threshold_minutes: int | None = None) -> dict[str, list[int]]:
    """Vuelve a despachar las corridas `pending` sin señales de vida.

    La espera se mide desde el último despacho (``metadata["dispatched_at"]``,
    ver ``apps.skill.dispatch``), o desde la creación en las corridas
    anteriores a ese sello. Reencolar es seguro porque el runner reclama la
    corrida con un UPDATE atómico. Tras ``MAX_AUTO_REQUEUES`` intentos sin que
    arranque, pasa a `failed`: ya no es un problema de cola.
    """
    from datetime import datetime

    from apps.skill.dispatch import dispatch_execution

    minutes = threshold_minutes if threshold_minutes is not None else PENDING_THRESHOLD_MINUTES
    cutoff = timezone.now() - timedelta(minutes=minutes)
    requeued: list[int] = []
    failed: list[int] = []

    for execution in SkillExecution.objects.filter(status=ExecutionStatus.PENDING).only(
        "id", "metadata", "created_at"
    ):
        metadata = dict(execution.metadata or {})
        try:
            since = datetime.fromisoformat(metadata["dispatched_at"])
        except (KeyError, TypeError, ValueError):
            since = execution.created_at
        if since >= cutoff:
            continue
        attempts = int(metadata.get("auto_requeues") or 0)
        if attempts >= MAX_AUTO_REQUEUES:
            SkillExecution.objects.filter(pk=execution.pk, status=ExecutionStatus.PENDING).update(
                status=ExecutionStatus.FAILED,
                finished_at=timezone.now(),
                error_message=(
                    f"La corrida no arrancó tras {attempts + 1} despachos. Revisar "
                    "que el worker interactivo esté corriendo y volver a lanzarla."
                ),
            )
            failed.append(execution.pk)
            continue
        metadata["auto_requeues"] = attempts + 1
        SkillExecution.objects.filter(pk=execution.pk).update(metadata=metadata)
        dispatch_execution(execution.pk)
        requeued.append(execution.pk)

    if requeued or failed:
        logger.warning("Corridas pending reencoladas: %s; fallidas: %s", requeued, failed)
    return {"requeued": requeued, "failed": failed}
