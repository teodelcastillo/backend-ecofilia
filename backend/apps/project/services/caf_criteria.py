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
# Su mirada sobre el financiamiento verde: los componentes que cuenta como verdes
# (los que sugirió el insumo, aceptados o descartados, y los propios) y las
# oportunidades de enverdecimiento que recomienda o descarta.
ENVERDECIMIENTO_KEY = "enverdecimiento"

_METADATA_KEYS = {"actualizado_en", "actualizado_por"}

_INSUMO_RESPUESTAS = {
    "considerado": "lo tuvo en cuenta",
    "no_aplica": "considera que no aplica",
}


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _list_of_dicts(value) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def executive_enverdecimiento(notes) -> dict:
    """El enverdecimiento del ejecutivo, sin metadatos. Vacío si no lo completó."""
    raw = _dict(_dict(notes).get(ENVERDECIMIENTO_KEY))
    componentes = _list_of_dicts(raw.get("componentes"))
    oportunidades = _list_of_dicts(raw.get("oportunidades"))
    if not componentes and not oportunidades:
        return {}
    return {"componentes": componentes, "oportunidades": oportunidades}


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
        "enverdecimiento": executive_enverdecimiento(notes),
    }


def _insumo_respuesta(value) -> tuple[str, str]:
    if isinstance(value, dict):
        return _text(value.get("estado")), _text(value.get("nota"))
    return _text(value), ""


def guided_basis(notes) -> dict:
    """El criterio con el que se genera un informe guiado, para detectar después
    si el ejecutivo lo cambió (y los borradores quedaron desactualizados).

    Es lo que el guiado efectivamente usa: determinaciones, fundamentos,
    consideraciones y enverdecimiento. La interfaz lo compara con lo actual.
    """
    assessment = executive_assessment(notes)
    return {
        "determinaciones": assessment["determinaciones"],
        "fundamentos": assessment["fundamentos"],
        "consideraciones_adicionales": assessment["consideraciones_adicionales"],
        "enverdecimiento": assessment["enverdecimiento"],
    }


def ecofilia_determinations(execution) -> dict[str, dict]:
    """La conclusión de Ecofilia por criterio, de la síntesis (``S.1.``) de su IET.

    Devuelve ``{clave: {"determinacion", "fundamento"}}`` con las claves de
    ``CRITERIOS``. Vacío si la corrida no tiene síntesis.
    """
    if execution is None:
        return {}
    by_label = {label: key for key, label in CRITERIOS}
    for step in (execution.output_structured or {}).get("steps") or []:
        if not str(step.get("title") or "").strip().startswith("S.1"):
            continue
        table = step.get("table") or {}
        rows = table.get("rows") if isinstance(table, dict) else None
        out: dict[str, dict] = {}
        for row in _list_of_dicts(rows):
            key = by_label.get(_text(row.get("criterio")))
            if key and _text(row.get("determinacion")):
                out[key] = {
                    "determinacion": _text(row.get("determinacion")),
                    "fundamento": _text(row.get("fundamento")),
                }
        return out
    return {}


def _name(value) -> str:
    return " ".join(_text(value).lower().split())


def _amount(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _usd(value) -> str:
    amount = _amount(value)
    return f"USD {amount:,.0f}" if amount is not None else "monto sin desagregar"


def _enverdecimiento_lines(
    enverdecimiento: dict, insumo_componentes: list[dict], insumo_oportunidades: list[dict]
) -> tuple[list[str], bool]:
    """Su enverdecimiento contra lo que sugirió el insumo, y si difiere."""
    componentes = enverdecimiento.get("componentes") or []
    oportunidades = enverdecimiento.get("oportunidades") or []
    sugeridos = {_name(row.get("componente")): row for row in insumo_componentes}
    differs = False
    lines: list[str] = []

    incluidos = [c for c in componentes if _text(c.get("estado")) != "descartado"]
    descartados = [c for c in componentes if _text(c.get("estado")) == "descartado"]
    if incluidos:
        lines.append("Componentes que el ejecutivo cuenta como financiamiento verde:")
    for comp in incluidos:
        line = (
            f"- {_text(comp.get('componente'))} — {_text(comp.get('objetivo')) or 'sin objetivo'}"
            f"{' — ' + _text(comp.get('categoria')) if _text(comp.get('categoria')) else ''}"
            f" — {_usd(comp.get('monto_usd'))}."
        )
        if _text(comp.get("fundamento")):
            line += f" Fundamento: {_text(comp.get('fundamento'))}"
        sugerido = sugeridos.get(_name(comp.get("componente")))
        if _text(comp.get("origen")) == "ejecutivo" or sugerido is None:
            line += " (lo sumó el ejecutivo; el insumo no lo sugería)"
            differs = True
        elif _text(sugerido.get("objetivo")) != _text(comp.get("objetivo")) or (
            _amount(sugerido.get("monto_usd")) != _amount(comp.get("monto_usd"))
        ):
            line += (
                f" (el insumo lo sugería como {_text(sugerido.get('objetivo'))}, "
                f"{_usd(sugerido.get('monto_usd'))})"
            )
            differs = True
        lines.append(line)
    for comp in descartados:
        line = f"- Descartado por el ejecutivo: {_text(comp.get('componente'))}."
        if _text(comp.get("fundamento")):
            line += f" Por qué: {_text(comp.get('fundamento'))}"
        lines.append(line)
        differs = True

    recomendadas = [o for o in oportunidades if _text(o.get("estado")) == "recomendar"]
    descartadas = [o for o in oportunidades if _text(o.get("estado")) == "descartar"]
    for label, items in (
        ("Oportunidades de enverdecimiento que recomienda", recomendadas),
        ("Oportunidades que descarta", descartadas),
    ):
        if not items:
            continue
        lines.append(f"{label}:")
        for opp in items:
            line = f"- {_text(opp.get('oportunidad'))}"
            if _text(opp.get("nota")):
                line += f" ({_text(opp.get('nota'))})"
            lines.append(line)
    if descartadas:
        differs = True

    if lines and insumo_componentes:
        lines.append(
            "Lo que sugería el insumo de Ecofilia: "
            + "; ".join(
                f"{_text(row.get('componente'))} ({_text(row.get('objetivo'))}, {_usd(row.get('monto_usd'))})"
                for row in insumo_componentes
            )
            + "."
        )
    if lines and insumo_oportunidades:
        lines.append(
            "Oportunidades que sugería el insumo: "
            + "; ".join(_text(row.get("oportunidad")) for row in insumo_oportunidades)
            + "."
        )
    return lines, differs


def build_criterio_ejecutivo(
    notes,
    *,
    ecofilia: dict[str, dict] | None = None,
    insumo_componentes: list[dict] | None = None,
    insumo_oportunidades: list[dict] | None = None,
) -> str:
    """El criterio del ejecutivo como texto, para el workflow guiado.

    Va como parámetro de la corrida y no por el contexto de la operación: las
    corridas de Ecofilia no tienen que verlo (ver ``operation_context``), y el
    guiado tiene que verlo completo, criterio por criterio.

    Al lado de cada determinación va la de Ecofilia (``ecofilia``, de la síntesis
    de su IET) con la marca «coincide» o «DIFIERE»: el guiado sólo reescribe las
    secciones donde difieren. El enverdecimiento se compara con lo que sugirió
    el insumo.
    """
    assessment = executive_assessment(notes)
    determinaciones = assessment["determinaciones"]
    fundamentos = assessment["fundamentos"]
    insumo = assessment["insumo"]
    ecofilia = ecofilia or {}

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
        theirs = ecofilia.get(key)
        if theirs:
            marca = "coincide" if valor and valor == theirs["determinacion"] else "DIFIERE"
            line += f" Ecofilia concluyó: {theirs['determinacion']}"
            if theirs.get("fundamento"):
                line += f" ({theirs['fundamento']})"
            line += f". → {marca}."
        lines.append(line)

    green_lines, green_differs = _enverdecimiento_lines(
        assessment["enverdecimiento"], insumo_componentes or [], insumo_oportunidades or []
    )

    if not lines and not green_lines:
        return ""
    parts: list[str] = []
    if lines:
        parts.append("Determinaciones del ejecutivo, criterio por criterio:\n" + "\n".join(lines))
    if green_lines:
        marca = "DIFIERE del insumo" if green_differs else "coincide con el insumo"
        parts.append(
            f"Enverdecimiento según el ejecutivo (→ {marca}):\n" + "\n".join(green_lines)
        )
    if assessment["consideraciones_adicionales"]:
        parts.append(
            "Consideraciones adicionales del ejecutivo (aspectos que tuvo en cuenta "
            "además de los criterios técnicos):\n" + assessment["consideraciones_adicionales"]
        )
    return "\n\n".join(parts)
