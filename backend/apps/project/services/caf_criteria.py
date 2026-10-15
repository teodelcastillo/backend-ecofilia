"""
Los criterios de alineación con París que evalúa el IET DATBC.

Son los mismos que la pestaña Alineación del portal (``ecofiliaV2/lib/
caf-alineacion.ts``): cuatro por el Objetivo 1, seis por el Objetivo 2,
contando las dos determinaciones. Las claves son las de
``context_notes["alineacion_paris"]``.

Las migraciones que siembran workflows copian estas listas en vez de
importarlas: una migración tiene que poder correr igual dentro de un año, aunque
este módulo cambie.
"""
from __future__ import annotations

# (clave, etiqueta)
CRITERIOS: list[tuple[str, str]] = [
    ("ct_a1", "CT A1 — Análisis de riesgos climáticos"),
    ("ct_a2", "CT A2 — Análisis de medidas"),
    ("ct_a3", "CT A3 — Consistencia con los instrumentos nacionales de adaptación"),
    ("objetivo_1", "Objetivo 1 — Desarrollo resiliente al clima"),
    ("ct_m1", "CT M1 — Actividades consideradas no alineadas"),
    ("ct_m2", "CT M2 — Actividades alineadas de manera directa"),
    ("ct_m3", "CT M3 — Consistencia con la NDC"),
    ("ct_m4", "CT M4 — Consistencia con trayectorias de desarrollo bajo en emisiones"),
    ("ct_m5", "CT M5 — Análisis de riesgos de transición"),
    ("objetivo_2", "Objetivo 2 — Desarrollo bajo en emisiones de GEI"),
]

CRITERIO_LABELS: dict[str, str] = dict(CRITERIOS)

# Claves de `context_notes` donde el ejecutivo deja su mirada.
ALINEACION_KEY = "alineacion_paris"
FUNDAMENTOS_KEY = "alineacion_fundamentos"
INSUMO_KEY = "alineacion_insumo"
CONSIDERACIONES_KEY = "consideraciones_adicionales"

_METADATA_KEYS = {"actualizado_en", "actualizado_por"}

_INSUMO_RESPUESTAS = {
    "considerado": "lo tuvo en cuenta",
    "no_aplica": "considera que no aplica",
}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def executive_assessment(notes) -> dict:
    """La mirada estructurada del ejecutivo, tal como está en la operación."""
    notes = _dict(notes)
    determinaciones = {
        key: _text(value)
        for key, value in _dict(notes.get(ALINEACION_KEY)).items()
        if key not in _METADATA_KEYS and _text(value)
    }
    fundamentos = {
        key: _text(value)
        for key, value in _dict(notes.get(FUNDAMENTOS_KEY)).items()
        if _text(value)
    }
    insumo = {
        key: value
        for key, value in _dict(notes.get(INSUMO_KEY)).items()
        if isinstance(value, (str, dict)) and value
    }
    return {
        "determinaciones": determinaciones,
        "fundamentos": fundamentos,
        "insumo": insumo,
        "consideraciones_adicionales": _text(notes.get(CONSIDERACIONES_KEY)),
    }


def _insumo_respuesta(value) -> tuple[str, str]:
    if isinstance(value, dict):
        return _text(value.get("estado")), _text(value.get("nota"))
    return _text(value), ""


def build_criterio_ejecutivo(notes) -> str:
    """El criterio del ejecutivo como texto, para el workflow guiado.

    Va como parámetro de la corrida y no por el contexto de la operación: las
    corridas de Ecofilia no tienen que verlo (ver ``operation_context``), y el
    guiado tiene que verlo completo, criterio por criterio.
    """
    assessment = executive_assessment(notes)
    determinaciones = assessment["determinaciones"]
    fundamentos = assessment["fundamentos"]
    insumo = assessment["insumo"]

    lines: list[str] = []
    for key, label in CRITERIOS:
        valor = determinaciones.get(key)
        fundamento = fundamentos.get(key)
        if not valor and not fundamento:
            continue
        line = f"- {label}: {valor or 'sin determinación'}."
        if fundamento:
            line += f" Fundamento del ejecutivo: {fundamento}"
        estado, nota = _insumo_respuesta(insumo.get(key))
        if estado in _INSUMO_RESPUESTAS:
            line += f" Sobre el insumo de Ecofilia para este criterio, el ejecutivo {_INSUMO_RESPUESTAS[estado]}"
            line += f" ({nota})." if nota else "."
        lines.append(line)

    if not lines:
        return ""
    text = "Determinaciones del ejecutivo, criterio por criterio:\n" + "\n".join(lines)
    if assessment["consideraciones_adicionales"]:
        text += (
            "\n\nConsideraciones adicionales del ejecutivo (aspectos que tuvo en cuenta "
            "además de los criterios técnicos):\n" + assessment["consideraciones_adicionales"]
        )
    return text
