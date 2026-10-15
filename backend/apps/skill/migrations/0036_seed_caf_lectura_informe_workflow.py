"""
Alta del workflow que lee el informe que el ejecutivo escribió por fuera.

Cuando el ejecutivo redacta su IDO afuera y lo sube, ese documento pasa a ser
el oficial. Ecofilia no lo corrige: lo lee para tener su mirada en la misma
forma que el resto —determinación y fundamento por criterio, recomendaciones,
y lo que el informe considera además de los criterios técnicos—, que es lo que
permite compararla con la de Ecofilia y llevarla al tablero.

Corre sobre el documento solo (contexto ``document``), no sobre la operación: el
informe del ejecutivo no se suma al expediente, para que las corridas de
Ecofilia no lo lean como evidencia.

Las recomendaciones quedan como tabla porque en DEC se va a verificar si se
incorporaron. Las "consideraciones adicionales" son la semilla de los criterios
que el ejecutivo mira y Ecofilia todavía no.
"""
from django.db import migrations

SLUG = "caf-lectura-informe-ejecutivo"
CAF_ORG_SLUG = "caf"

NO_CONSTA = "No consta en el informe"

CRITERIOS = [
    "CT A1 — Análisis de riesgos climáticos",
    "CT A2 — Análisis de medidas",
    "CT A3 — Consistencia con los instrumentos nacionales de adaptación",
    "Objetivo 1 — Desarrollo resiliente al clima",
    "CT M1 — Actividades consideradas no alineadas",
    "CT M2 — Actividades alineadas de manera directa",
    "CT M3 — Consistencia con la NDC",
    "CT M4 — Consistencia con trayectorias de desarrollo bajo en emisiones",
    "CT M5 — Análisis de riesgos de transición",
    "Objetivo 2 — Desarrollo bajo en emisiones de GEI",
]

# Copia del vocabulario del portal (`ecofiliaV2/lib/caf-alineacion.ts`), más los
# cierres textuales del CT M5 y la ausencia del criterio en el informe.
DETERMINACIONES = [
    "Riesgo bajo", "Riesgo medio", "Riesgo alto", "Indeterminado",
    "Sin análisis de riesgos (AR)", "No aplica análisis de riesgos (AR)",
    "Suficientes", "No suficientes", "Información insuficiente", "No aplica",
    "Consistencia", "Potencialmente inconsistente",
    "Alineada", "Con brechas por CT A1", "Con brechas por CT A2", "Con brechas por CT A3",
    "No alineada",
    "Sin actividades no alineadas", "Contiene actividades no alineadas",
    "Contiene actividades alineadas", "Sin actividades alineadas",
    "No persisten riesgos de transición", "Persisten riesgos de transición",
    "Alineada en el CT M2", "Alineada en el CT M3", "Alineada en el CT M4",
    "Alineada en el CT M5", "Con brechas",
    NO_CONSTA,
]

SYSTEM_PROMPT = (
    "Tu nombre es Ecofilia. Leés el Informe de Evaluación Técnica que redactó un "
    "ejecutivo de CAF sobre una operación y registrás lo que dice, sin juzgarlo: "
    "sus determinaciones por criterio, sus recomendaciones y lo que considera "
    "además de los criterios técnicos.\n\n"
    "Registrás lo que el informe dice, no lo que vos concluirías. Si el informe no "
    "trata algo, lo anotás como ausente; no lo completás. Escribís en español, en "
    "registro técnico, y citás la sección del informe de la que sale cada dato."
)

STEPS = [
    (
        "1. Determinaciones por criterio",
        "Para cada uno de los diez criterios, registrá la determinación que adopta "
        "el informe y el fundamento que da. Una fila por criterio, en el orden de la "
        "lista, sin saltear ninguno.\n\n"
        "Usá el valor que corresponda al criterio entre los admitidos. Si el informe "
        f"no trata el criterio o no llega a una determinación, usá «{NO_CONSTA}».",
        "table",
        {
            "name": "Determinaciones del informe",
            "description": "La determinación y el fundamento que da el informe en cada criterio.",
            "columns": [
                {"key": "criterio", "label": "Criterio", "type": "enum", "required": True,
                 "allowed_values": CRITERIOS},
                {"key": "determinacion", "label": "Determinación", "type": "enum",
                 "required": True, "allowed_values": DETERMINACIONES},
                {"key": "fundamento", "label": "Fundamento", "type": "text", "required": True,
                 "prompt_hint": "Lo que dice el informe para sostenerla, en no más de 35 palabras."},
            ],
        },
    ),
    (
        "2. Recomendaciones",
        "Listá todas las recomendaciones que hace el informe, una por fila, con su "
        "destinatario y el criterio al que se refieren. Si una recomendación es "
        "general, usá «General». Si el informe no hace recomendaciones, devolvé la "
        "tabla vacía.",
        "table",
        {
            "name": "Recomendaciones del informe",
            "description": "Lo que el informe recomienda, para verificarlo después en DEC.",
            "columns": [
                {"key": "recomendacion", "label": "Recomendación", "type": "text", "required": True},
                {"key": "destinatario", "label": "Destinatario", "type": "enum", "required": True,
                 "allowed_values": ["CESAS", "Área de negocio", "Cliente", "Otro o no indicado"]},
                {"key": "criterio", "label": "Criterio", "type": "enum", "required": True,
                 "allowed_values": CRITERIOS + ["General"]},
            ],
        },
    ),
    (
        "3. Consideraciones adicionales",
        "Listá los aspectos, riesgos o criterios que el informe tiene en cuenta además "
        "de los diez criterios técnicos de alineación (por ejemplo, salvaguardas "
        "sociales, capacidad del ejecutor o riesgos de implementación). Una viñeta "
        "por aspecto, con la sección del informe donde aparece. Si no hay ninguno, "
        "respondé «Ninguna».",
        "text",
        {},
    ),
]


def create_lectura_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")
    Organization = apps.get_model("user", "Organization")

    skill = Skill.objects.filter(slug=SLUG).first()
    if skill is None:
        iet = Skill.objects.filter(slug="caf-iet-datbc-evaluacion-tecnica").first()
        skill = Skill.objects.create(
            owner=None,
            skill_type="copilot",
            name="Lectura del informe del ejecutivo",
            slug=SLUG,
            description=(
                "Lee el informe que el ejecutivo subió y registra sus determinaciones, "
                "recomendaciones y consideraciones adicionales."
            ),
            allowed_contexts=["document"],
            system_prompt=SYSTEM_PROMPT,
            prompt_template="",
            model=(iet.model if iet else ""),
            temperature=0.0,
            retrieval_strategy=(iet.retrieval_strategy if iet else "global"),
            k_per_doc=(iet.k_per_doc if iet else 2),
            total_limit=(iet.total_limit if iet else 12),
            members_can_run=False,
        )
        SkillStep.objects.bulk_create(
            [
                SkillStep(
                    skill=skill,
                    title=title,
                    instructions=instructions,
                    position=position,
                    step_type="instruction",
                    output_mode=output_mode,
                    table_schema=schema,
                    output_validation="strict",
                    evidence_selection="all",
                    evidence_tags=[],
                    evidence_mode="documents",
                    approval_required=False,
                    comparative_enabled=False,
                )
                for position, (title, instructions, output_mode, schema) in enumerate(STEPS, start=1)
            ]
        )

    org = Organization.objects.filter(slug=CAF_ORG_SLUG).first()
    if org is not None:
        org.enabled_skills.add(skill)


def remove_lectura_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Skill.objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0035_seed_caf_iet_guiado_workflow"),
    ]

    operations = [
        migrations.RunPython(create_lectura_workflow, remove_lectura_workflow),
    ]
