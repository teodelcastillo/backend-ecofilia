"""
Agrega a la IET la Sección C (enverdecimiento) y, al final, la síntesis
estructurada por criterio.

**C.1.** La IET no redactaba la Sección C: la completaba el ejecutivo. Ahora la
redacta Ecofilia tomando como guía el insumo (componentes que podrían calificar
a financiamiento verde y oportunidades), así su informe y sus sugerencias de
enverdecimiento no se contradicen, y el informe guiado puede reescribir C igual
que D y E. Se inserta después del último paso B; los siguientes se corren una
posición.

**S.1.**
La IET evalúa cada criterio técnico en prosa ("D.2. CT A1 — …"), y cada paso
cierra con una conclusión textual. Para comparar la mirada de Ecofilia con la
del ejecutivo hace falta esa conclusión en el mismo vocabulario que usa el
ejecutivo en la pestaña Alineación (``ecofiliaV2/lib/caf-alineacion.ts``).

El paso no vuelve a analizar: lee los pasos D ya redactados y traduce cada
conclusión a una determinación del vocabulario, con un fundamento corto. Suma
"No determinable con la información disponible" para que una falta de
información no se fuerce a una determinación (la diferencia entre brecha y
falta de información que pidió la DATBC).

Su título empieza con "S." para que el informe no lo muestre: es un dato para
la comparación, no una sección del IET. Se agrega una sola vez, al final, y sólo
mira los pasos D que existan cuando corre la migración.
"""
from django.db import migrations

IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
TITLE = "S.1. Síntesis por criterio"

NO_DETERMINABLE = "No determinable con la información disponible"

# (criterio, valores admitidos). Copia del vocabulario del portal.
VOCABULARIO: list[tuple[str, list[str]]] = [
    (
        "CT A1 — Análisis de riesgos climáticos",
        [
            "Riesgo bajo", "Riesgo medio", "Riesgo alto", "Indeterminado",
            "Sin análisis de riesgos (AR)", "No aplica análisis de riesgos (AR)",
        ],
    ),
    (
        "CT A2 — Análisis de medidas",
        ["Suficientes", "No suficientes", "Información insuficiente", "No aplica"],
    ),
    (
        "CT A3 — Consistencia con los instrumentos nacionales de adaptación",
        ["Consistencia", "Potencialmente inconsistente"],
    ),
    (
        "Objetivo 1 — Desarrollo resiliente al clima",
        [
            "Alineada", "Con brechas por CT A1", "Con brechas por CT A2",
            "Con brechas por CT A3", "No alineada",
        ],
    ),
    (
        "CT M1 — Actividades consideradas no alineadas",
        ["Sin actividades no alineadas", "Contiene actividades no alineadas"],
    ),
    (
        "CT M2 — Actividades alineadas de manera directa",
        ["Contiene actividades alineadas", "Sin actividades alineadas"],
    ),
    ("CT M3 — Consistencia con la NDC", ["Consistencia", "Potencialmente inconsistente"]),
    (
        "CT M4 — Consistencia con trayectorias de desarrollo bajo en emisiones",
        ["Consistencia", "Potencialmente inconsistente"],
    ),
    (
        "CT M5 — Análisis de riesgos de transición",
        ["No persisten riesgos de transición", "Persisten riesgos de transición"],
    ),
    (
        "Objetivo 2 — Desarrollo bajo en emisiones de GEI",
        [
            "Alineada en el CT M2", "Alineada en el CT M3", "Alineada en el CT M4",
            "Alineada en el CT M5", "Con brechas", "No alineada",
        ],
    ),
]


def _all_values() -> list[str]:
    seen: list[str] = []
    for _, values in VOCABULARIO:
        for value in values:
            if value not in seen:
                seen.append(value)
    seen.append(NO_DETERMINABLE)
    return seen


SCHEMA = {
    "name": "Síntesis por criterio",
    "description": "La conclusión de cada criterio en el vocabulario del ejecutivo.",
    "columns": [
        {
            "key": "criterio",
            "label": "Criterio",
            "type": "enum",
            "required": True,
            "allowed_values": [criterio for criterio, _ in VOCABULARIO],
        },
        {
            "key": "determinacion",
            "label": "Determinación",
            "type": "enum",
            "required": True,
            "allowed_values": _all_values(),
            "prompt_hint": "Uno de los valores admitidos para ese criterio.",
        },
        {
            "key": "fundamento",
            "label": "Fundamento",
            "type": "text",
            "required": True,
            "prompt_hint": "Por qué, en no más de 35 palabras, tomado del paso que lo analizó.",
        },
    ],
}

INSTRUCTIONS = (
    "Traducí a una determinación cada criterio que ya analizaron los pasos "
    "anteriores. No vuelvas a analizar: tomá la conclusión del paso que trató el "
    "criterio y elegí el valor que le corresponde.\n\n"
    "Una fila por criterio, en este orden, con estos valores admitidos:\n"
    + "\n".join(f"- {criterio}: {' / '.join(values)}" for criterio, values in VOCABULARIO)
    + "\n\nEquivalencias con las conclusiones del informe:\n"
    "- «Consistencia o al menos no inconsistencia» (con o sin brechas) → "
    "Consistencia; «Inconsistencia explícita» o «Potencialmente inconsistente» → "
    "Potencialmente inconsistente. Si había brechas, decilo en el fundamento.\n"
    "- CT A1: «Sin evaluación de riesgos» o «Sin información de CESAS» → "
    "Sin análisis de riesgos (AR); «No aplica (PBLs, SWAps)» → No aplica análisis "
    "de riesgos (AR).\n"
    "- CT A2: «Las medidas permiten reducir de manera aceptable los riesgos» → "
    "Suficientes; «NO permiten» → No suficientes.\n"
    "- Objetivos: «Con Brechas para su Alineación» → la opción de brechas que "
    "corresponda al criterio que la causó.\n\n"
    f"Si el paso no llegó a una conclusión por falta de información, usá "
    f"«{NO_DETERMINABLE}» y nombrá lo que faltaba en el fundamento. No conviertas "
    "una falta de información en una brecha."
)


C_TITLE = "C.1. Actividades elegibles para financiamiento verde y co-beneficios"
C_INSTRUCTIONS = (
    "Redactá la evaluación preliminar de los co-beneficios ambientales, climáticos y "
    "de biodiversidad de la operación, en línea con los Criterios y Actividades "
    "Elegibles de Financiamiento Verde de CAF.\n\n"
    "Tomá como guía el insumo de Ecofilia que acompaña esta corrida (componentes con "
    "potencial de financiamiento verde y oportunidades de enverdecimiento): "
    "presentá los componentes que califican o podrían calificar, con su objetivo "
    "(adaptación, mitigación, o biodiversidad y otros objetivos ambientales), su "
    "categoría y su monto cuando el expediente lo permite, y cerrá con las "
    "recomendaciones para aumentar los co-beneficios.\n\n"
    "Si los documentos llevan a una conclusión distinta de la del insumo, decilo y "
    "explicá por qué. Citá el documento de cada dato."
)


def _shift_after(SkillStep, skill, after_position: int, delta: int) -> None:
    """Corre una posición los pasos después de ``after_position`` y sus referencias.

    `position` es única por skill: se mueve de a uno, en el orden que no choca.
    Los pasos que eligen a mano qué pasos previos ven (`history_positions`)
    siguen apuntando a los mismos pasos.
    """
    steps = list(SkillStep.objects.filter(skill=skill, position__gt=after_position))
    steps.sort(key=lambda s: s.position, reverse=delta > 0)
    for step in steps:
        step.position += delta
        step.save(update_fields=["position"])
    for step in SkillStep.objects.filter(skill=skill):
        positions = step.history_positions or []
        moved = [p + delta if p > after_position else p for p in positions]
        if moved != positions:
            step.history_positions = moved
            step.save(update_fields=["history_positions"])


def add_c_and_sintesis(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")

    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return

    steps = list(SkillStep.objects.filter(skill=skill).order_by("position"))
    # ── Sección C: después del último paso B ──
    if not any((step.title or "").strip().startswith("C.") for step in steps):
        b_positions = [s.position for s in steps if (s.title or "").strip().startswith("B.")]
        after = max(b_positions) if b_positions else 0
        _shift_after(SkillStep, skill, after, +1)
        SkillStep.objects.create(
            skill=skill,
            title=C_TITLE,
            instructions=C_INSTRUCTIONS,
            format_instructions="",
            position=after + 1,
            step_type="instruction",
            output_mode="text",
            output_validation="lenient",
            evidence_selection="tagged",
            evidence_tags=["documento-operacion", "metodologia-caf"],
            evidence_mode="both",
            approval_required=False,
            comparative_enabled=False,
        )
        steps = list(SkillStep.objects.filter(skill=skill).order_by("position"))

    # ── Síntesis por criterio: al final, leyendo sólo los pasos D ──
    if any((step.title or "").strip().startswith("S.1.") for step in steps):
        return
    d_positions = [step.position for step in steps if (step.title or "").strip().startswith("D.")]
    SkillStep.objects.create(
        skill=skill,
        title=TITLE,
        instructions=INSTRUCTIONS,
        format_instructions="",
        position=(steps[-1].position + 1) if steps else 1,
        step_type="instruction",
        output_mode="table",
        table_schema=SCHEMA,
        output_validation="strict",
        # Integra lo ya redactado: no lee documentos. Se declara como los demás
        # pasos que integran, para que ninguno quede "leyendo todo" por omisión.
        evidence_selection="blueprint_only",
        evidence_tags=[],
        evidence_mode="previous_steps",
        history_mode="selected" if d_positions else "auto",
        history_positions=d_positions,
        approval_required=False,
        comparative_enabled=False,
    )


def remove_c_and_sintesis(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")
    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return
    SkillStep.objects.filter(skill=skill, title=TITLE).delete()
    c_step = SkillStep.objects.filter(skill=skill, title=C_TITLE).first()
    if c_step is not None:
        position = c_step.position
        c_step.delete()
        _shift_after(SkillStep, skill, position, -1)


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0033_seed_caf_insumo_workflow"),
    ]

    operations = [
        migrations.RunPython(add_c_and_sintesis, remove_c_and_sintesis),
    ]
