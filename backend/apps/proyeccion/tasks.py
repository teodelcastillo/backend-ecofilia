"""Poner a correr un análisis o una ficha sin que la persona espere el pedido.

Con cola (producción) van a Celery, por la cola interactiva si existe: son
trabajos con alguien mirando la pantalla, y la cola por defecto la puede tener
tomada una ingesta de documentos. Sin cola (local) corren en un hilo.
"""
from __future__ import annotations

import logging
import threading

from celery import shared_task
from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)


@shared_task(name="proyeccion.analizar")
def analizar_task(proyeccion_id: int) -> None:
    from apps.proyeccion.analisis import analizar

    analizar(proyeccion_id)


@shared_task(name="proyeccion.generar_ficha")
def generar_ficha_task(ficha_id: int) -> None:
    from apps.proyeccion.analisis import generar_ficha

    generar_ficha(ficha_id)


def _en_hilo(tarea, pk: int) -> None:
    try:
        tarea(pk)
    finally:
        connections.close_all()


def _despachar(tarea, pk: int) -> None:
    if getattr(settings, "CELERY_BROKER_URL", None):
        opciones = {}
        if getattr(settings, "INTERACTIVE_SQS_QUEUE_URL", None):
            opciones["queue"] = settings.CELERY_TASK_INTERACTIVE_QUEUE
        tarea.apply_async(args=[pk], **opciones)
        return
    threading.Thread(target=_en_hilo, args=(tarea, pk), daemon=True).start()


def despachar_analisis(proyeccion_id: int) -> None:
    _despachar(analizar_task, proyeccion_id)


def despachar_ficha(ficha_id: int) -> None:
    _despachar(generar_ficha_task, ficha_id)
