"""
Agrega a la IET un último paso: la síntesis estructurada por criterio.

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


def add_sintesis(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")

    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return
    steps = list(SkillStep.objects.filter(skill=skill).order_by("position"))
    if any((step.title or "").strip().startswith("S.1.") for step in steps):
        return
    # Sólo los pasos D: los que concluyen cada criterio y cada objetivo.
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


def remove_sintesis(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")
    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return
    SkillStep.objects.filter(skill=skill, title=TITLE).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0033_seed_caf_insumo_workflow"),
    ]

    operations = [
        migrations.RunPython(add_sintesis, remove_sintesis),
    ]
