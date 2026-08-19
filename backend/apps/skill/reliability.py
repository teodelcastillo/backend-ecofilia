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
