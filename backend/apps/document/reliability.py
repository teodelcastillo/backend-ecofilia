"""Rescatar documentos que se quedaron en `pending` y nunca arrancaron.

`pending` significa "encolado, esperando worker". El problema es que hasta
acá nada verificaba nunca que ese mensaje existiera: el despacho era un
disparo al aire. Si el mensaje se perdió —SQS rechazó el encolado, la cola se
purgó, el worker estuvo caído mientras expiraba el mensaje, el ruteo apuntaba
a una cola sin consumidor— el documento se quedaba en `pending` para siempre.
Nada lo reintentaba, nada lo marcaba como roto, y la interfaz seguía diciendo
"en proceso" indefinidamente.

`acks_late` no cubre este caso: sólo reentrega mensajes que un worker tomó y
no confirmó. Un mensaje que nunca existió no se reentrega.

Este módulo es la red: cada pocos minutos busca documentos en `pending` sin
señales de vida y los vuelve a despachar. Volver a despachar es seguro porque
``process_document_chunks`` reclama la fila con un UPDATE atómico — si el
mensaje original sí existía y llega tarde, el segundo despacho sale sin hacer
nada en vez de duplicar chunks.

No reintenta para siempre: después de ``DOC_MAX_AUTO_REQUEUES`` pasadas el
documento pasa a ERROR. Un documento que no arranca tras tres despachos no
tiene un problema de cola, y dejarlo rotando esconde el problema real.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.document.dispatch import dispatch_processing
from apps.document.models import ChunkingStatus, Document

logger = logging.getLogger(__name__)

# Un worker sano toma un mensaje de SQS en segundos; el long polling está en 20
# y la cola de ingesta puede tener una tanda por delante. Quince minutos no es
# "va lento", es "ese mensaje no está".
STUCK_PENDING_MINUTES = int(os.environ.get("DOC_STUCK_PENDING_MINUTES", "15"))
MAX_AUTO_REQUEUES = int(os.environ.get("DOC_MAX_AUTO_REQUEUES", "3"))


def requeue_stuck_documents(*, threshold_minutes: int | None = None) -> dict[str, list[int]]:
    """Vuelve a despachar los `pending` abandonados. Devuelve qué hizo con cada uno."""
    minutes = (
        threshold_minutes if threshold_minutes is not None else STUCK_PENDING_MINUTES
    )
    cutoff = timezone.now() - timedelta(minutes=minutes)

    stuck = (
        Document.objects
        .filter(chunking_status=ChunkingStatus.PENDING)
        # `status_changed_at` es nulo en los documentos anteriores a este campo;
        # para esos `created_at` es la mejor señal disponible. Sin este respaldo
        # los documentos ya trabados —justo los que motivan el módulo— quedarían
        # invisibles para siempre.
        .annotate(pending_since=Coalesce("status_changed_at", "created_at"))
        .filter(pending_since__lt=cutoff)
        .order_by("id")
    )

    result: dict[str, list[int]] = {"requeued": [], "failed": [], "exhausted": []}

    for doc in stuck:
        if not doc.file:
            # Nunca va a procesarse: sin archivo la tarea falla en cada intento.
            Document.objects.filter(pk=doc.pk).update(
                chunking_status=ChunkingStatus.ERROR,
                status_changed_at=timezone.now(),
                last_error="El documento no tiene un archivo asociado.",
            )
            result["failed"].append(doc.pk)
            continue

        if doc.requeue_count >= MAX_AUTO_REQUEUES:
            Document.objects.filter(pk=doc.pk).update(
                chunking_status=ChunkingStatus.ERROR,
                status_changed_at=timezone.now(),
                last_error=(
                    f"El procesamiento no arrancó después de {doc.requeue_count} "
                    "reenvíos automáticos a la cola. Revisar los logs del worker "
                    "de ingesta (/ecs/ecofilia-worker) y reprocesar a mano."
                ),
            )
            result["exhausted"].append(doc.pk)
            continue

        # Sellar antes de despachar: si el mensaje vuelve a perderse, la próxima
        # pasada cuenta desde ahora y no reenvía en bucle cada cinco minutos.
        Document.objects.filter(pk=doc.pk).update(
            status_changed_at=timezone.now(),
            requeue_count=doc.requeue_count + 1,
        )
        if dispatch_processing(doc.pk):
            result["requeued"].append(doc.pk)
        else:
            # dispatch_processing ya dejó el documento en ERROR con el motivo.
            result["failed"].append(doc.pk)

    if any(result.values()):
        logger.warning(
            "Documentos trabados en pending: reenviados=%s, agotados=%s, fallidos=%s",
            result["requeued"], result["exhausted"], result["failed"],
        )
    return result


# El caso que el reaper de `pending` no veía: el worker reclamó el documento
# (PROCESSING) y murió a mitad —el 2026-09-28 el documento 928, un PDF de
# Colombia con mucha imagen, mató al worker de ingesta por memoria a los 20
# segundos—. `acks_late` reentrega el mensaje, pero la tarea ve PROCESSING y
# sale con `already_claimed`: el documento queda "procesándose" para siempre, y
# el chequeo previo de las corridas bloquea cualquier workflow sobre la
# operación que lo contenga. El documento más largo que vimos tardó una hora;
# cuatro horas sin terminar no es "va lento".
STUCK_PROCESSING_MINUTES = int(os.environ.get("DOC_STUCK_PROCESSING_MINUTES", "240"))


def requeue_stalled_processing(*, threshold_minutes: int | None = None) -> dict[str, list[int]]:
    """Vuelve a `pending` y reencola los documentos que un worker muerto dejó en PROCESSING.

    Reprocesar es seguro: la tarea borra los chunks del documento antes de
    escribir los nuevos. Comparte el contador con el reaper de `pending`, así
    que un documento que tumba al worker cada vez termina en ERROR con el
    motivo, en vez de rotar para siempre.
    """
    minutes = (
        threshold_minutes if threshold_minutes is not None else STUCK_PROCESSING_MINUTES
    )
    cutoff = timezone.now() - timedelta(minutes=minutes)
    stalled = (
        Document.objects
        .filter(chunking_status=ChunkingStatus.PROCESSING)
        .annotate(processing_since=Coalesce("status_changed_at", "created_at"))
        .filter(processing_since__lt=cutoff)
        .order_by("id")
    )

    result: dict[str, list[int]] = {"requeued": [], "failed": [], "exhausted": []}
    for doc in stalled:
        if doc.requeue_count >= MAX_AUTO_REQUEUES:
            Document.objects.filter(pk=doc.pk).update(
                chunking_status=ChunkingStatus.ERROR,
                status_changed_at=timezone.now(),
                last_error=(
                    f"El procesamiento se cortó {doc.requeue_count + 1} veces sin "
                    "terminar. Lo más probable es que el worker de ingesta se quede "
                    "sin memoria con este archivo (/ecs/ecofilia-worker, buscar "
                    "SIGKILL). Reprocesar a mano cuando se resuelva."
                ),
            )
            result["exhausted"].append(doc.pk)
            continue

        # Condicionado al estado: si la tarea terminó entre la consulta y acá,
        # no se pisa su resultado.
        reset = Document.objects.filter(
            pk=doc.pk, chunking_status=ChunkingStatus.PROCESSING
        ).update(
            chunking_status=ChunkingStatus.PENDING,
            status_changed_at=timezone.now(),
            requeue_count=doc.requeue_count + 1,
        )
        if not reset:
            continue
        if dispatch_processing(doc.pk):
            result["requeued"].append(doc.pk)
        else:
            result["failed"].append(doc.pk)

    if any(result.values()):
        logger.warning(
            "Documentos trabados en processing: reenviados=%s, agotados=%s, fallidos=%s",
            result["requeued"], result["exhausted"], result["failed"],
        )
    return result
