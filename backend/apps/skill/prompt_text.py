"""
Los rótulos que el motor escribe en el pedido de cada paso, en el idioma de la
plataforma.

El resto del pedido —la base documental, sus reglas, el estándar de
entregable— ya estaba en castellano, pero los rótulos que lo estructuran
(«Task», «Instructions», «Run parameters»…) habían quedado en inglés. Un
pedido en dos idiomas le da al modelo una señal ambigua sobre en cuál
responder, y mezcla el del autor del workflow con el nuestro.

Viven acá, y no dispersos por el motor, para que el idioma se decida en un
solo lugar: ``PLATFORM_LANGUAGE``. Hoy la plataforma es sólo en castellano, así
que hay un único juego de rótulos; un idioma nuevo es un diccionario más, y
mientras no esté completo se cae al castellano en vez de mezclar.
"""
from __future__ import annotations

import os

DEFAULT_LANGUAGE = "es"

_TEXT: dict[str, dict[str, str]] = {
    "es": {
        "task": "## Tarea: {title}",
        "instructions": "Instrucciones: {instructions}",
        "reasoning": "## Criterios y metodología\n{text}",
        "run_parameters": "## Parámetros de la corrida:\n{lines}",
        "extra_instructions": "Instrucciones adicionales de quien lanzó la corrida: {text}",
        "previous_sections": "## Secciones previas de este informe:",
        "research_scratchpad": "## Relevamiento previo del expediente:\n{text}",
        "document_context": "## Contexto documental para esta sección:\n{text}",
        "no_document_content": (
            "(No se encontró contenido documental para esta sección: "
            "indicalo en tu respuesta.)"
        ),
        "comparative_header": "## Requisitos comparativos:",
        "comparative_intro": "Requisitos de la salida comparativa:",
        "comparative_by_document": (
            "1) Presentá los hallazgos documento por documento en cada criterio."
        ),
        "comparative_cover_all": (
            "2) En cada criterio incluí todos los documentos activos, aunque no "
            "haya evidencia directa."
        ),
        "comparative_strict": (
            "3) Si un documento no tiene evidencia para un criterio, escribí "
            "explícitamente: 'Sin evidencia en fuentes provistas'."
        ),
        "comparative_lenient": (
            "3) Si falta evidencia, declaralo con claridad y evitá inferencias "
            "sin sustento."
        ),
        "format": "## Formato de la respuesta\n{text}",
    },
}


def platform_language() -> str:
    language = os.environ.get("PLATFORM_LANGUAGE", DEFAULT_LANGUAGE).strip().lower()
    return language if language in _TEXT else DEFAULT_LANGUAGE


def text(key: str, **values) -> str:
    """El rótulo ``key`` en el idioma de la plataforma, con sus valores."""
    template = _TEXT[platform_language()].get(key) or _TEXT[DEFAULT_LANGUAGE][key]
    return template.format(**values) if values else template
