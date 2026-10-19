"""
La IET deja de recibir el insumo como guía y pasa a ser la fuente del insumo.

El insumo de Ecofilia ya no es un agente aparte: es la ficha visual que el
portal arma a partir de la IET (ver ``0041``). Tres cambios en la IET, siempre
ubicando los pasos por título y nunca por posición (la IET de producción puede
haberse ajustado desde el Studio); los 18 pasos originales no se tocan.

- **C.1** deja de apuntar al insumo: misma consigna (componentes que califican
  con su objetivo, categoría y monto; recomendaciones; citar), sin guía externa.
- **S.1 Síntesis por criterio** gana la columna ``informacion_faltante`` (lo que
  el ejecutivo ve como «Falta de información» en la ficha), el valor
  «Inconsistente» para CT A3, M3 y M4 y las reglas de los dos objetivos, que ahora se calculan a
  partir de los CT (la misma tabla que ``caf_criteria.determinar_objetivo_1``).
- **S.2 Componentes con potencial de financiamiento verde**: paso nuevo, oculto
  (título con «S.»), al final, que lee sólo C.1 y deja como tabla estricta los
  componentes que C.1 presenta como elegibles. Reemplaza a las tablas del insumo
  como fuente estructurada del enverdecimiento sugerido.
"""
from django.db import migrations

IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"

C_TITLE = "C.1. Actividades elegibles para financiamiento verde y co-beneficios"
S1_TITLE = "S.1. Síntesis por criterio"
S2_TITLE = "S.2. Componentes con potencial de financiamiento verde"

# ── C.1 ──────────────────────────────────────────────────────────────────
C_INSUMO_PARRAFO = (
    "Tomá como guía el insumo de Ecofilia que acompaña esta corrida (componentes con "
    "potencial de financiamiento verde y oportunidades de enverdecimiento): "
    "presentá los componentes que califican o podrían calificar, con su objetivo "
    "(adaptación, mitigación, o biodiversidad y otros objetivos ambientales), su "
    "categoría y su monto cuando el expediente lo permite, y cerrá con las "
    "recomendaciones para aumentar los co-beneficios."
)
C_PARRAFO = (
    "Presentá los componentes que califican o podrían calificar, con su objetivo "
    "(adaptación, mitigación, o biodiversidad y otros objetivos ambientales), su "
    "categoría y su monto cuando el expediente lo permite, y cerrá con las "
    "recomendaciones para aumentar los co-beneficios."
)
C_INSUMO_CIERRE = (
    "Si los documentos llevan a una conclusión distinta de la del insumo, decilo y "
    "explicá por qué. Citá el documento de cada dato."
)
C_CIERRE = "Citá el documento de cada dato."

# ── S.1 ──────────────────────────────────────────────────────────────────
CT_A3 = "CT A3 — Consistencia con los instrumentos nacionales de adaptación"
INCONSISTENTE = "Inconsistente"

INFORMACION_FALTANTE_COLUMN = {
    "key": "informacion_faltante",
    "label": "Información faltante",
    "type": "text",
    "required": False,
    "prompt_hint": (
        "Qué información del expediente faltó para evaluar el criterio, en no más "
        "de 25 palabras. Vacío si no faltó nada."
    ),
}

REGLAS_OBJETIVO_1 = (
    "\n\nReglas del Objetivo 1 (se deriva de los CT; no lo decidas aparte):\n"
    "- CT A3 «Inconsistente» → No alineada.\n"
    "- CT A3 «Potencialmente inconsistente» → Con brechas por CT A3.\n"
    "- CT A1 «Riesgo bajo» o «No aplica análisis de riesgos (AR)» → CT A2 «No "
    "aplica»; con CT A3 «Consistencia» → Alineada.\n"
    "- CT A1 «Riesgo medio» o «Riesgo alto» con CT A2 «No suficientes» o "
    "«Información insuficiente» → Con brechas por CT A2; con CT A2 «Suficientes» "
    "y CT A3 «Consistencia» → Alineada.\n"
    "- CT A1 «Indeterminado» o «Sin análisis de riesgos (AR)» (y CT A2 sin brecha) "
    "→ Con brechas por CT A1.\n"
    "Precedencia: CT A3 Inconsistente, CT A3 Potencialmente inconsistente, CT A2, "
    "CT A1, Alineada. CT A3 «Inconsistente» es una inconsistencia explícita con los "
    "instrumentos nacionales; «Potencialmente inconsistente», una posible.\n\n"
    "En «informacion_faltante» nombrá lo que faltó del expediente para evaluar el "
    "criterio (o dejalo vacío): es lo que el ejecutivo ve como falta de información."
)

CT_M3 = "CT M3 — Consistencia con la NDC"
CT_M4 = "CT M4 — Consistencia con trayectorias de desarrollo bajo en emisiones"
CONSISTENCIA_LINEAS = (CT_A3, CT_M3, CT_M4)

REGLAS_OBJETIVO_2 = (
    "\n\nReglas del Objetivo 2 (se deriva de los CT, en este orden; donde uno decide, "
    "el análisis termina y los CT siguientes no aplican):\n"
    "- CT M1 «Contiene actividades no alineadas» → No alineada.\n"
    "- CT M2 «Contiene actividades alineadas» → Alineada en el CT M2.\n"
    "- CT M3 «Consistencia» (o al menos no inconsistencia con la NDC) → Alineada en el "
    "CT M3; «Inconsistente» → No alineada; «Potencialmente inconsistente» → sigue al CT M4.\n"
    "- CT M4 «Inconsistente» → No alineada; «Consistencia» o «Potencialmente "
    "inconsistente» → sigue al CT M5.\n"
    "- CT M5 «No persisten riesgos de transición» → Alineada en el CT M5; «Persisten "
    "riesgos de transición» → Con brechas."
)

# ── S.2 ──────────────────────────────────────────────────────────────────
OBJETIVO_VALUES = ["Adaptación", "Mitigación", "Biodiversidad y otros objetivos ambientales"]

S2_SCHEMA = {
    "name": "Componentes con potencial de financiamiento verde",
    "description": "Los componentes que la Sección C presenta como elegibles o con potencial.",
    "columns": [
        {"key": "componente", "label": "Componente", "type": "text", "required": True},
        {
            "key": "objetivo",
            "label": "Objetivo",
            "type": "enum",
            "required": True,
            "allowed_values": OBJETIVO_VALUES,
        },
        {
            "key": "categoria",
            "label": "Categoría CAF",
            "type": "text",
            "required": False,
            "prompt_hint": "Categoría de los Criterios y Actividades Elegibles de CAF, si C.1 la nombra.",
        },
        {
            "key": "monto_usd",
            "label": "Monto USD",
            "type": "number",
            "required": False,
            "prompt_hint": "Monto en dólares, sin separadores ni símbolo. Vacío si C.1 no lo desagrega.",
        },
        {
            "key": "fundamento",
            "label": "Fundamento",
            "type": "text",
            "required": True,
            "prompt_hint": "Por qué califica, en no más de 35 palabras, tomado de C.1.",
        },
    ],
}

S2_INSTRUCTIONS = (
    "Pasá a una tabla los componentes que la Sección C.1 presenta como elegibles o "
    "con potencial de financiamiento verde. No vuelvas a analizar el expediente: "
    "una fila por componente, con el objetivo (Adaptación, Mitigación, o "
    "Biodiversidad y otros objetivos ambientales), la categoría CAF y el monto "
    "cuando C.1 los dice, y un fundamento corto tomado de C.1.\n\n"
    "Usá el nombre del componente tal como aparece en la operación. Si C.1 no "
    "identifica ningún componente elegible, devolvé la tabla vacía."
)


def _steps(SkillStep, skill):
    return list(SkillStep.objects.filter(skill=skill).order_by("position"))


def _by_title(steps, title):
    return next((s for s in steps if (s.title or "").strip() == title), None)


def _c1_sin_insumo(step) -> None:
    text = step.instructions or ""
    if C_INSUMO_PARRAFO in text:
        text = text.replace(C_INSUMO_PARRAFO, C_PARRAFO)
    if C_INSUMO_CIERRE in text:
        text = text.replace(C_INSUMO_CIERRE, C_CIERRE)
    if text != step.instructions:
        step.instructions = text
        step.save(update_fields=["instructions"])


def _c1_con_insumo(step) -> None:
    text = step.instructions or ""
    if C_PARRAFO in text and C_INSUMO_PARRAFO not in text:
        text = text.replace(C_PARRAFO, C_INSUMO_PARRAFO)
    if C_CIERRE in text and C_INSUMO_CIERRE not in text:
        text = text.replace(C_CIERRE, C_INSUMO_CIERRE)
    if text != step.instructions:
        step.instructions = text
        step.save(update_fields=["instructions"])


def _s1_enriquecer(step) -> None:
    schema = dict(step.table_schema or {})
    columns = [dict(c) for c in schema.get("columns") or []]
    keys = [c.get("key") for c in columns]
    if "informacion_faltante" not in keys:
        columns.append(dict(INFORMACION_FALTANTE_COLUMN))
    for column in columns:
        if column.get("key") == "determinacion":
            values = list(column.get("allowed_values") or [])
            if INCONSISTENTE not in values:
                column["allowed_values"] = values + [INCONSISTENTE]
    schema["columns"] = columns
    step.table_schema = schema

    text = step.instructions or ""
    # Las líneas del vocabulario de consistencia (CT A3, M3, M4) suman «Inconsistente».
    for ct in CONSISTENCIA_LINEAS:
        linea_vieja = f"- {ct}: Consistencia / Potencialmente inconsistente\n"
        linea_nueva = f"- {ct}: Consistencia / Potencialmente inconsistente / Inconsistente\n"
        if linea_vieja in text:
            text = text.replace(linea_vieja, linea_nueva)
    if REGLAS_OBJETIVO_1.strip() not in text:
        text = text + REGLAS_OBJETIVO_1
    if REGLAS_OBJETIVO_2.strip() not in text:
        text = text + REGLAS_OBJETIVO_2
    step.instructions = text
    step.save(update_fields=["table_schema", "instructions"])


def _s1_restaurar(step) -> None:
    schema = dict(step.table_schema or {})
    columns = [dict(c) for c in schema.get("columns") or [] if c.get("key") != "informacion_faltante"]
    for column in columns:
        if column.get("key") == "determinacion":
            column["allowed_values"] = [
                v for v in (column.get("allowed_values") or []) if v != INCONSISTENTE
            ]
    schema["columns"] = columns
    step.table_schema = schema

    text = step.instructions or ""
    text = text.replace(REGLAS_OBJETIVO_2, "").replace(REGLAS_OBJETIVO_1, "")
    for ct in CONSISTENCIA_LINEAS:
        linea_nueva = f"- {ct}: Consistencia / Potencialmente inconsistente / Inconsistente\n"
        linea_vieja = f"- {ct}: Consistencia / Potencialmente inconsistente\n"
        text = text.replace(linea_nueva, linea_vieja)
    step.instructions = text
    step.save(update_fields=["table_schema", "instructions"])


def aplicar(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")
    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return
    steps = _steps(SkillStep, skill)

    c1 = _by_title(steps, C_TITLE)
    if c1 is not None:
        _c1_sin_insumo(c1)

    s1 = _by_title(steps, S1_TITLE)
    if s1 is not None:
        _s1_enriquecer(s1)

    if _by_title(steps, S2_TITLE) is None:
        SkillStep.objects.create(
            skill=skill,
            title=S2_TITLE,
            instructions=S2_INSTRUCTIONS,
            format_instructions="",
            position=(steps[-1].position + 1) if steps else 1,
            step_type="instruction",
            output_mode="table",
            table_schema=S2_SCHEMA,
            output_validation="strict",
            evidence_selection="blueprint_only",
            evidence_tags=[],
            evidence_mode="previous_steps",
            history_mode="selected" if c1 is not None else "auto",
            history_positions=[c1.position] if c1 is not None else [],
            approval_required=False,
            comparative_enabled=False,
        )


def revertir(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")
    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return
    SkillStep.objects.filter(skill=skill, title=S2_TITLE).delete()
    steps = _steps(SkillStep, skill)
    s1 = _by_title(steps, S1_TITLE)
    if s1 is not None:
        _s1_restaurar(s1)
    c1 = _by_title(steps, C_TITLE)
    if c1 is not None:
        _c1_con_insumo(c1)


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0039_guiado_reescribe_seccion_e"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
