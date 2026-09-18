"""Encolar la ingesta de un documento sin perder el error si falla.

El incidente que motiva este módulo: un documento quedó en `pending` para
siempre. Cada sitio que subía un archivo hacía
``transaction.on_commit(lambda: process_document_chunks.delay(pk))`` y daba el
trabajo por encolado. Pero `.delay()` habla con SQS, y SQS puede fallar
(credenciales, red, throttling). Una excepción dentro de un callback de
``on_commit`` se levanta después de que la transacción ya se confirmó: la fila
del documento queda escrita, en PENDING, sin mensaje en la cola y sin nada en
`last_error` que explique por qué. Para la persona que subió el archivo eso se
ve exactamente igual que "todavía está en la fila".

Acá el fallo se persiste: el documento pasa a ERROR con el motivo. Un documento
visiblemente roto se puede reprocesar; uno invisiblemente roto no.
"""
from __future__ import annotations

import logging

from django.utils import timezone

from apps.document.models import ChunkingStatus, Document
from apps.document.tasks import process_document_chunks

logger = logging.getLogger(__name__)


def dispatch_processing(doc_id: int) -> bool:
    """Despacha la ingesta. Devuelve False (y marca ERROR) si no pudo encolar."""
    try:
        process_document_chunks.delay(doc_id)
        return True
    except Exception as exc:  # broker caído, credenciales, throttling…
        logger.exception("No se pudo encolar la ingesta del documento %s: %s", doc_id, exc)
        Document.objects.filter(pk=doc_id).update(
            chunking_status=ChunkingStatus.ERROR,
            status_changed_at=timezone.now(),
            last_error=(
                "No se pudo encolar el procesamiento del documento "
                f"({exc}). El archivo está guardado; volvé a intentar el "
                "reprocesamiento."
            ),
        )
        return False


def mark_pending(doc_id: int) -> None:
    """Deja el documento listo para una corrida nueva y sella el reloj.

    El sello es lo que después permite al reaper decidir si un PENDING está
    recién encolado o abandonado.
    """
    Document.objects.filter(pk=doc_id).update(
        chunking_status=ChunkingStatus.PENDING,
        status_changed_at=timezone.now(),
    )
