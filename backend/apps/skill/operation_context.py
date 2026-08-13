"""
Bloque de contexto de la operación para las ejecuciones de skills.

Un workflow de CAF siempre corre *sobre una operación*, y hasta ahora el
motor de skills no le contaba nada de ella al modelo: el prompt de cada paso
se armaba con el título, las instrucciones y los fragmentos recuperados, de
modo que el modelo no sabía de qué país hablaba, por cuánto era la operación,
ni cuál era su objetivo. El copiloto de chat sí inyectaba `context_notes`
(ver ``apps.chat.services.copilot``), pero ese código no lo comparte el motor
de skills.

Este módulo arma un bloque único con:

  1. Los datos que cargó el usuario al crear la operación (``context_notes``).
  2. El documento marcado como principal (blueprint) de esa operación.

El bloque es **idéntico para todos los pasos** de una corrida y se antepone
al system prompt, no al prompt de cada paso. Eso lo hace estable a lo largo
del workflow y, de paso, cacheable como prefijo.

Del documento principal se incluye su resumen (``content_summary``), no el
texto completo: el texto entero de un préstamo puede ser de cientos de
páginas, y repetirlo en cada paso desbordaría el contexto. El detalle fino ya
llega por la recuperación semántica, que está restringida a los documentos de
la operación.
"""
from __future__ import annotations

# Claves de `context_notes` que tienen una etiqueta cuidada, en el orden en
# que conviene leerlas. El resto se agrega después con su nombre crudo, para
# que un campo nuevo del formulario no quede invisible para el modelo.
_LABELS: list[tuple[str, str]] = [
    ("numero_operacion", "Número de operación"),
    ("estado", "Estado de avance"),
    ("pais", "País"),
    ("ubicacion_geografica", "Ubicación geográfica"),
    ("sector", "Sector"),
    ("modalidad", "Modalidad"),
    ("tipo_operacion", "Tipo de operación"),
    ("monto", "Monto CAF (millones de USD)"),
    ("year", "Año"),
    ("cliente", "Cliente"),
    ("ejecutor", "Ejecutor"),
    ("ecosistemas_estrategicos", "Ecosistemas estratégicos"),
    ("ecosistemas_estrategicos_otros", "Otros ecosistemas estratégicos"),
]

# Se presentan aparte, como bloques de texto, porque son párrafos y no datos.
_NARRATIVE_KEYS = ("objetivo", "componentes")

# Ruido para el análisis: identificadores internos y datos de tramitación.
_SKIP_KEYS = {
    "extra",
    "link_focus",
    "consecutivo",
    "fecha_informe",
    "representante_pais",
    "ejecutivo_pais",
    "ejecutivo_responsable",
    "ejecutivo_datbc",
}

_MAX_SUMMARY_CHARS = 4000

# `estado` se guarda como slug; sin esto el prompt decía "originacion".
_ESTADO_LABELS = {
    "originacion": "En originación",
    "evaluacion": "En evaluación",
    "aprobado": "Aprobado",
    "ejecucion": "En ejecución",
    "completado": "Completado",
}


def _clean(value) -> str:
    """
    Valor listo para el prompt.

    Las listas se aplanan con comas: los ecosistemas estratégicos se guardan
    como array desde el formulario, y sin esto llegarían al modelo como el
    `repr` de una lista de Python.
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [_clean(item) for item in value]
        return ", ".join(part for part in parts if part)
    if isinstance(value, bool):
        return "Sí" if value else "No"
    text = str(value).strip()
    return "" if text.lower() in ("", "none", "null") else text


def _format_notes(context_notes: dict) -> list[str]:
    notes = context_notes or {}
    lines: list[str] = []
    seen: set[str] = set()

    for key, label in _LABELS:
        value = _clean(notes.get(key))
        seen.add(key)
        if key == "estado":
            value = _ESTADO_LABELS.get(value, value)
        if value:
            lines.append(f"- {label}: {value}")

    for key, value in notes.items():
        if key in seen or key in _SKIP_KEYS or key in _NARRATIVE_KEYS:
            continue
        cleaned = _clean(value)
        if cleaned:
            lines.append(f"- {key.replace('_', ' ').capitalize()}: {cleaned}")

    return lines


def _format_blueprint(project) -> list[str]:
    """Identidad y resumen del documento principal de la operación."""
    document = getattr(project, "blueprint_document", None)
    if document is None:
        return [
            "",
            "## Documento principal de la operación",
            "La operación todavía no tiene un documento marcado como principal. "
            "Basate únicamente en los documentos disponibles en el contexto.",
        ]

    lines = ["", "## Documento principal de la operación", f"- Título: {document.name}"]

    description = _clean(getattr(document, "description", ""))
    if description:
        lines.append(f"- Descripción: {description}")

    summary = _clean(getattr(document, "content_summary", ""))
    if summary:
        if len(summary) > _MAX_SUMMARY_CHARS:
            summary = summary[:_MAX_SUMMARY_CHARS].rstrip() + "…"
        lines.extend(["", "Resumen del documento principal:", summary])

    return lines


def build_operation_context_block(project) -> str:
    """
    Bloque de contexto de la operación, o cadena vacía si no hay operación.

    Se antepone al system prompt de cada paso, de modo que todo el workflow
    comparte exactamente el mismo contexto.
    """
    if project is None:
        return ""

    lines = [
        "# Operación bajo análisis",
        "",
        "Todo tu análisis se refiere a esta operación. Usá estos datos como "
        "contexto de base en cada paso: son los que cargó el equipo al dar de "
        "alta la operación.",
        "",
        f"- Nombre de la operación: {project.name}",
    ]

    description = _clean(getattr(project, "description", ""))
    if description:
        lines.append(f"- Descripción: {description}")

    lines.extend(_format_notes(getattr(project, "context_notes", None)))

    notes = getattr(project, "context_notes", None) or {}
    for key in _NARRATIVE_KEYS:
        value = _clean(notes.get(key))
        if value:
            heading = "Objetivo de la operación" if key == "objetivo" else "Componentes / Actividades"
            lines.extend(["", f"## {heading}", value])

    lines.extend(_format_blueprint(project))

    lines.extend(
        [
            "",
            "Toda afirmación sobre la operación o sobre el marco normativo debe "
            "estar respaldada por los documentos asociados a esta operación. No "
            "completes con conocimiento general ni con supuestos: si un dato no "
            "está en los documentos, decilo explícitamente.",
        ]
    )

    return "\n".join(lines)
