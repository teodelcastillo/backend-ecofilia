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
# (los que sugirió Ecofilia en su IET —``S.2.``—, aceptados o descartados, y los
# propios) y las oportunidades de enverdecimiento propias que recomienda.
ENVERDECIMIENTO_KEY = "enverdecimiento"

# ── Reglas de determinación ────────────────────────────────────────────
# Los objetivos no se eligen: se derivan de sus CT con el procedimiento de la
# DATBC. Es la misma tabla que aplica el portal (``ecofiliaV2/lib/caf-alineacion.ts``)
# y la que S.1 de la IET recibe en sus instrucciones; cualquier cambio va en los
# tres. El recorrido termina donde un CT decide: los CT que no se alcanzan
# quedan en «No aplica».
A1_SIN_RIESGO = ("Riesgo bajo", "No aplica análisis de riesgos (AR)")
A1_CON_RIESGO = ("Riesgo medio", "Riesgo alto")
A1_SIN_ANALISIS = ("Indeterminado", "Sin análisis de riesgos (AR)")
A2_SUFICIENTES = "Suficientes"
A2_NO_APLICA = "No aplica"
A2_CON_BRECHA = ("No suficientes", "Información insuficiente")
A3_CONSISTENCIA = "Consistencia"
A3_POTENCIALMENTE_INCONSISTENTE = "Potencialmente inconsistente"
A3_INCONSISTENTE = "Inconsistente"

OBJETIVO_1_ALINEADA = "Alineada"
OBJETIVO_1_BRECHAS_A1 = "Con brechas por CT A1"
OBJETIVO_1_BRECHAS_A2 = "Con brechas por CT A2"
OBJETIVO_1_BRECHAS_A3 = "Con brechas por CT A3"
OBJETIVO_1_NO_ALINEADA = "No alineada"


def ct_a2_no_aplica(ct_a1) -> bool:
    """Con riesgo bajo o sin análisis de riesgos aplicable, no hay medidas que evaluar."""
    return _text(ct_a1) in A1_SIN_RIESGO


M1_NO_ALINEADAS = "Contiene actividades no alineadas"
M1_SIN_NO_ALINEADAS = "Sin actividades no alineadas"
M2_ALINEADAS = "Contiene actividades alineadas"
M2_SIN_ALINEADAS = "Sin actividades alineadas"
M5_SIN_RIESGOS = "No persisten riesgos de transición"
M5_CON_RIESGOS = "Persisten riesgos de transición"

OBJETIVO_2_ALINEADA_M2 = "Alineada en el CT M2"
OBJETIVO_2_ALINEADA_M3 = "Alineada en el CT M3"
OBJETIVO_2_ALINEADA_M5 = "Alineada en el CT M5"
OBJETIVO_2_BRECHAS = "Con brechas"
OBJETIVO_2_NO_ALINEADA = "No alineada"

CT_OBJETIVO_2 = ("ct_m1", "ct_m2", "ct_m3", "ct_m4", "ct_m5")


def determinar_objetivo_2(ct_m1, ct_m2, ct_m3, ct_m4, ct_m5) -> tuple[str | None, str, tuple[str, ...]]:
    """La determinación del Objetivo 2 que sale de los CT, por qué, y qué CT no aplican.

    Un recorrido: M1 con actividades no alineadas → No alineada. M2 con
    actividades alineadas → Alineada en el CT M2. M3 consistente → Alineada en
    el CT M3; inconsistente → No alineada; potencialmente inconsistente → M4.
    M4 inconsistente → No alineada; si no → M5. M5 sin riesgos de transición →
    Alineada en el CT M5; persisten → Con brechas. Devuelve ``(None, motivo, ())``
    mientras falte el CT que decide.
    """
    m1, m2, m3, m4, m5 = (_text(v) for v in (ct_m1, ct_m2, ct_m3, ct_m4, ct_m5))

    def fin(valor, motivo, desde):
        return valor, motivo, CT_OBJETIVO_2[CT_OBJETIVO_2.index(desde) + 1 :]

    if not m1:
        return None, "Falta el CT M1.", ()
    if m1 == M1_NO_ALINEADAS:
        return fin(OBJETIVO_2_NO_ALINEADA, "CT M1: la operación contiene actividades de la lista de no alineadas.", "ct_m1")
    if m1 != M1_SIN_NO_ALINEADAS:
        return None, f"CT M1 «{m1}» no permite determinar el Objetivo 2.", ()
    if not m2:
        return None, "Falta el CT M2.", ()
    if m2 == M2_ALINEADAS:
        return fin(OBJETIVO_2_ALINEADA_M2, "CT M2: la operación concuerda con la lista de actividades alineadas.", "ct_m2")
    if m2 != M2_SIN_ALINEADAS:
        return None, f"CT M2 «{m2}» no permite determinar el Objetivo 2.", ()
    if not m3:
        return None, "Falta el CT M3: sin actividades de la lista, se evalúa la consistencia con la NDC.", ()
    if m3 == A3_CONSISTENCIA:
        return fin(OBJETIVO_2_ALINEADA_M3, "CT M3: consistente (o al menos no inconsistente) con la NDC.", "ct_m3")
    if m3 == A3_INCONSISTENTE:
        return fin(OBJETIVO_2_NO_ALINEADA, "CT M3: inconsistente con la NDC.", "ct_m3")
    if m3 != A3_POTENCIALMENTE_INCONSISTENTE:
        return None, f"CT M3 «{m3}» no permite determinar el Objetivo 2.", ()
    if not m4:
        return None, "Falta el CT M4: con la NDC potencialmente inconsistente, se evalúa la trayectoria de largo plazo.", ()
    if m4 == A3_INCONSISTENTE:
        return fin(OBJETIVO_2_NO_ALINEADA, "CT M4: inconsistente con la estrategia de largo plazo.", "ct_m4")
    if m4 not in (A3_CONSISTENCIA, A3_POTENCIALMENTE_INCONSISTENTE):
        return None, f"CT M4 «{m4}» no permite determinar el Objetivo 2.", ()
    if not m5:
        return None, "Falta el CT M5: el análisis termina en los riesgos de transición.", ()
    if m5 == M5_SIN_RIESGOS:
        return fin(OBJETIVO_2_ALINEADA_M5, f"CT M4 «{m4}» y no persisten riesgos de transición.", "ct_m5")
    if m5 == M5_CON_RIESGOS:
        return fin(OBJETIVO_2_BRECHAS, "CT M5: persisten riesgos de transición.", "ct_m5")
    return None, f"CT M5 «{m5}» no permite determinar el Objetivo 2.", ()


def determinar_objetivo_1(ct_a1, ct_a2, ct_a3) -> tuple[str | None, str]:
    """La determinación del Objetivo 1 que sale de los CT, y por qué.

    Precedencia: CT A3 Inconsistente > CT A3 Potencialmente inconsistente >
    CT A2 > CT A1 > Alineada. El análisis siempre termina en CT A3: sin CT A3
    no hay determinación (``(None, motivo)``). Con CT A1 bajo o sin AR
    aplicable, CT A2 se da por «No aplica», diga lo que diga.
    """
    a1, a2, a3 = _text(ct_a1), _text(ct_a2), _text(ct_a3)
    if not a3:
        return None, "Falta el CT A3: el análisis del Objetivo 1 termina ahí."
    if a3 == A3_INCONSISTENTE:
        return OBJETIVO_1_NO_ALINEADA, "CT A3: inconsistente con los instrumentos nacionales de adaptación."
    if a3 == A3_POTENCIALMENTE_INCONSISTENTE:
        return OBJETIVO_1_BRECHAS_A3, "CT A3: potencialmente inconsistente con los instrumentos nacionales."
    if a3 != A3_CONSISTENCIA:
        return None, f"CT A3 «{a3}» no permite determinar el Objetivo 1."
    if ct_a2_no_aplica(a1):
        return OBJETIVO_1_ALINEADA, f"CT A1 «{a1}»: no hacen falta medidas (CT A2 no aplica) y hay consistencia en CT A3."
    if a1 in A1_CON_RIESGO:
        if a2 in A2_CON_BRECHA:
            return OBJETIVO_1_BRECHAS_A2, f"CT A1 «{a1}» con medidas «{a2}»."
        if a2 == A2_SUFICIENTES:
            return OBJETIVO_1_ALINEADA, f"CT A1 «{a1}» con medidas suficientes y consistencia en CT A3."
        return None, "Falta el CT A2: con riesgo medio o alto hay que evaluar las medidas."
    if a1 in A1_SIN_ANALISIS:
        if a2 in A2_CON_BRECHA:
            return OBJETIVO_1_BRECHAS_A2, f"CT A1 «{a1}» y medidas «{a2}»."
        return OBJETIVO_1_BRECHAS_A1, f"CT A1 «{a1}»: el riesgo climático no está evaluado."
    if not a1:
        return None, "Falta el CT A1."
    return None, f"CT A1 «{a1}» no permite determinar el Objetivo 1."

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
    objetivo_1_motivo, objetivo_2_motivo = _aplicar_reglas(determinaciones)
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
        "objetivo_1_motivo": objetivo_1_motivo,
        "objetivo_2_motivo": objetivo_2_motivo,
        "consideraciones_adicionales": _text(notes.get(CONSIDERACIONES_KEY)),
        "enverdecimiento": executive_enverdecimiento(notes),
    }


def _aplicar_reglas(determinaciones: dict[str, str]) -> tuple[str, str]:
    """Deja en ``determinaciones`` lo que dictan los CT: los que el recorrido no
    alcanza en «No aplica» y cada objetivo con su valor. Devuelve los motivos
    (vacío cuando los CT no alcanzan para determinar: queda lo que había)."""
    if ct_a2_no_aplica(determinaciones.get("ct_a1")):
        determinaciones["ct_a2"] = A2_NO_APLICA
    valor_1, motivo_1 = determinar_objetivo_1(
        determinaciones.get("ct_a1"), determinaciones.get("ct_a2"), determinaciones.get("ct_a3")
    )
    if valor_1 is not None:
        determinaciones["objetivo_1"] = valor_1
    valor_2, motivo_2, no_aplican = determinar_objetivo_2(*(determinaciones.get(k) for k in CT_OBJETIVO_2))
    for key in no_aplican:
        determinaciones[key] = A2_NO_APLICA
    if valor_2 is not None:
        determinaciones["objetivo_2"] = valor_2
    return (motivo_1 if valor_1 is not None else ""), (motivo_2 if valor_2 is not None else "")


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

    Devuelve ``{clave: {"determinacion", "fundamento", "informacion_faltante"}}``
    con las claves de ``CRITERIOS``. Vacío si la corrida no tiene síntesis.

    Los objetivos se recalculan con las reglas a partir de los CT de la misma
    síntesis: si la prosa decía otra cosa, lo que dijo queda en
    ``objetivo_1_texto`` / ``objetivo_2_texto`` y el fundamento pasa a ser el
    motivo de la regla.
    """
    if execution is None:
        return {}
    by_label = {label: key for key, label in CRITERIOS}
    for step in _steps(execution):
        if not str(step.get("title") or "").strip().startswith("S.1"):
            continue
        out: dict[str, dict] = {}
        for row in _table_rows(step):
            key = by_label.get(_text(row.get("criterio")))
            if key and _text(row.get("determinacion")):
                out[key] = {
                    "determinacion": _text(row.get("determinacion")),
                    "fundamento": _text(row.get("fundamento")),
                    "informacion_faltante": _text(row.get("informacion_faltante")),
                }
        _recalcular_objetivos(out)
        return out
    return {}


def _recalcular_objetivos(out: dict[str, dict]) -> None:
    def valor(key):
        return (out.get(key) or {}).get("determinacion")

    valor_1, motivo_1 = determinar_objetivo_1(valor("ct_a1"), valor("ct_a2"), valor("ct_a3"))
    valor_2, motivo_2, _ = determinar_objetivo_2(*(valor(k) for k in CT_OBJETIVO_2))
    for key, valor_regla, motivo in (("objetivo_1", valor_1, motivo_1), ("objetivo_2", valor_2, motivo_2)):
        if valor_regla is None:
            continue
        previo = out.get(key) or {}
        texto = _text(previo.get("determinacion"))
        out[key] = {
            "determinacion": valor_regla,
            "fundamento": motivo,
            "informacion_faltante": _text(previo.get("informacion_faltante")),
            **({f"{key}_texto": texto} if texto and texto != valor_regla else {}),
        }


def _steps(execution) -> list[dict]:
    return _list_of_dicts((execution.output_structured or {}).get("steps"))


def _table_rows(step: dict) -> list[dict]:
    table = step.get("table") or {}
    return _list_of_dicts(table.get("rows")) if isinstance(table, dict) else []


def ecofilia_green_components(execution) -> list[dict]:
    """Los componentes con potencial de financiamiento verde según la IET de
    Ecofilia: la tabla de ``S.2.``, sin las marcas de cita de sus celdas. Vacío
    si la corrida no la tiene (IET anterior a la 0040)."""
    from apps.skill.citations import strip_cell_citations

    if execution is None:
        return []
    for step in _steps(execution):
        if not str(step.get("title") or "").strip().startswith("S.2"):
            continue
        return [
            {k: strip_cell_citations(v) if isinstance(v, str) else v for k, v in row.items()}
            for row in _table_rows(step)
            if _text(row.get("componente"))
        ]
    return []


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
    enverdecimiento: dict, sugeridos_ecofilia: list[dict]
) -> tuple[list[str], bool]:
    """Su enverdecimiento contra lo que sugirió Ecofilia (``S.2.`` de su IET), y si difiere.

    En el texto se sigue hablando del «insumo»: es el nombre que el ejecutivo
    ve en el portal para la ficha de la IET, y la marca «DIFIERE del insumo» es
    la que buscan los pasos del guiado (ver ``0039``).
    """
    componentes = enverdecimiento.get("componentes") or []
    oportunidades = enverdecimiento.get("oportunidades") or []
    sugeridos = {_name(row.get("componente")): row for row in sugeridos_ecofilia}
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

    if lines and sugeridos_ecofilia:
        lines.append(
            "Lo que sugería el insumo de Ecofilia: "
            + "; ".join(
                f"{_text(row.get('componente'))} ({_text(row.get('objetivo'))}, {_usd(row.get('monto_usd'))})"
                for row in sugeridos_ecofilia
            )
            + "."
        )
    return lines, differs


def build_criterio_ejecutivo(
    notes,
    *,
    ecofilia: dict[str, dict] | None = None,
    componentes_ecofilia: list[dict] | None = None,
) -> str:
    """El criterio del ejecutivo como texto, para el workflow guiado.

    Va como parámetro de la corrida y no por el contexto de la operación: las
    corridas de Ecofilia no tienen que verlo (ver ``operation_context``), y el
    guiado tiene que verlo completo, criterio por criterio.

    Al lado de cada determinación va la de Ecofilia (``ecofilia``, de la síntesis
    de su IET) con la marca «coincide» o «DIFIERE»: el guiado sólo reescribe las
    secciones donde difieren. El enverdecimiento se compara con los componentes
    que sugirió Ecofilia (``componentes_ecofilia``, de ``S.2.`` de su IET).
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
        assessment["enverdecimiento"], componentes_ecofilia or []
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
