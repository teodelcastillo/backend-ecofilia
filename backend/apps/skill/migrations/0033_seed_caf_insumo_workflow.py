"""
Alta del workflow del insumo para el ejecutivo.

Al entrar a IDO, Ecofilia genera dos cosas en paralelo: su reporte (la IET) y
este insumo. El insumo no es el informe: es una ficha de dos páginas como
máximo que le dice al ejecutivo qué mirar antes de redactar el suyo —lectura de
la operación, contexto país, elegibilidad verde y, criterio por criterio, los
puntos clave y la información que falta—. En el portal aparece como la primera
sección del informe IET, después de la carátula.

**No determina la alineación.** Si el insumo dijera "CT A2: medidas
insuficientes", el ejecutivo arrancaría anclado en esa conclusión y la
comparación posterior entre su mirada y la de Ecofilia mediría eso, no su
criterio. El veredicto de Ecofilia vive en su reporte.

La interfaz lee los pasos por el número de su título ("1." … "6.") y las tablas
por la clave de sus columnas. Los prompts se pueden ajustar desde el Studio;
esa estructura conviene mantenerla.

Se habilita en la organización CAF (para que sus usuarios vean el agente y las
operaciones nuevas lo traigan) y en las operaciones existentes que tienen la
IET, igual que hizo ``0028`` con el enverdecimiento.
"""
from django.db import migrations

SLUG = "caf-insumo-ejecutivo"
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
CAF_ORG_SLUG = "caf"

SYSTEM_PROMPT = (
    "Tu nombre es Ecofilia. Sos un analista técnico de la Dirección de Ambiente, "
    "Cambio Climático y Biodiversidad (DATBC) de CAF.\n\n"
    "Preparás un insumo para el ejecutivo que va a redactar el Informe de "
    "Evaluación Técnica (IET) de esta operación en la etapa de Inventario de "
    "Operaciones (IDO). El insumo no es el informe: es una ficha breve —dos "
    "páginas como máximo— que le dice al ejecutivo qué mirar.\n\n"
    "No determinás la alineación ni calificás los criterios: señalás lo relevante "
    "de los documentos, advertís lo que puede llevar a una mala lectura y "
    "declarás la información que falta. La decisión es del ejecutivo.\n\n"
    "Distinguí siempre una brecha de la operación de una falta de información en "
    "los documentos: no son lo mismo y llevan a determinaciones distintas.\n\n"
    "Escribís en español, en registro técnico y directo. Toda afirmación se apoya "
    "en los documentos de la operación; si un dato no está, decilo en lugar de "
    "suponerlo. Respetá el límite de extensión de cada paso: un insumo largo no "
    "se lee."
)

OBJETIVO_1_CRITERIOS = [
    "CT A1 — Análisis de riesgos climáticos",
    "CT A2 — Análisis de medidas",
    "CT A3 — Consistencia con los instrumentos nacionales de adaptación",
]
OBJETIVO_2_CRITERIOS = [
    "CT M1 — Actividades consideradas no alineadas",
    "CT M2 — Actividades alineadas de manera directa",
    "CT M3 — Consistencia con la NDC",
    "CT M4 — Consistencia con trayectorias de desarrollo bajo en emisiones",
    "CT M5 — Análisis de riesgos de transición",
]


def _criterios_schema(name: str, criterios: list[str]) -> dict:
    return {
        "name": name,
        "description": "Qué mirar en cada criterio técnico, sin determinarlo.",
        "columns": [
            {
                "key": "criterio",
                "label": "Criterio",
                "type": "enum",
                "required": True,
                "allowed_values": criterios,
            },
            {
                "key": "puntos_clave",
                "label": "Puntos clave y advertencias",
                "type": "text",
                "required": True,
                "prompt_hint": (
                    "Qué mirar en los documentos y qué advertir antes de determinar "
                    "el criterio. Máximo 45 palabras. Sin sugerir el resultado."
                ),
            },
            {
                "key": "informacion_faltante",
                "label": "Información faltante",
                "type": "text",
                "required": True,
                "prompt_hint": (
                    "Qué falta en los documentos para evaluarlo. Máximo 20 palabras. "
                    "'Nada relevante' si no falta nada."
                ),
            },
        ],
    }


_CRITERIOS_INSTRUCTIONS = (
    "Armá una fila por criterio, en el orden de la lista y sin saltear ninguno.\n\n"
    "En «puntos clave» poné lo que el ejecutivo tiene que mirar en los documentos "
    "para evaluar ese criterio en esta operación y lo que tiene que advertir antes "
    "de determinarlo. En «información faltante», lo que los documentos no dicen y "
    "haría falta.\n\n"
    "No determines el criterio ni sugieras su resultado. Si la operación muestra "
    "una brecha, nombrala como algo a revisar; si lo que hay es falta de "
    "información, decilo como falta de información."
)

# (título, instrucciones, formato, modo de salida, esquema, selección, etiquetas, evidencia)
STEPS = [
    (
        "1. Lectura de la operación",
        "Describí qué financia la operación: instrumento, componentes principales, "
        "ubicación y beneficiarios, según el documento principal.\n\n"
        "Cerrá con una oración sobre lo que el documento deja sin definir y que "
        "importa para la evaluación climática (por ejemplo, la ubicación de las "
        "obras o la tecnología a usar).",
        "Un párrafo de prosa. Máximo 120 palabras.",
        "text",
        {},
        "blueprint_only",
        [],
        "documents",
    ),
    (
        "2. Contexto país",
        "Resumí el contexto climático y de biodiversidad del país que importa para "
        "esta operación: los compromisos de la NDC y del Plan Nacional de "
        "Adaptación para su sector, las amenazas climáticas principales para la "
        "zona o el sector, y si el país tiene Estrategia de Largo Plazo.\n\n"
        "Indicá el instrumento del que sale cada dato. Dejá afuera lo que no se "
        "conecta con la operación.",
        "Prosa breve o hasta cuatro viñetas. Máximo 150 palabras.",
        "text",
        {},
        "tagged",
        ["ndc", "nap", "lts", "comunicacion-adaptacion", "nbsap"],
        "documents",
    ),
    (
        "3. Actividades elegibles para financiamiento verde",
        "Señalá qué componentes de la operación podrían calificar como "
        "financiamiento verde de CAF y bajo qué objetivo (adaptación, mitigación, "
        "o biodiversidad y otros objetivos ambientales), y qué información faltaría "
        "para confirmarlo.\n\n"
        "No estimes montos ni porcentajes: eso lo hace el análisis de "
        "enverdecimiento.",
        "Viñetas. Máximo 100 palabras.",
        "text",
        {},
        "blueprint_only",
        [],
        "documents",
    ),
    (
        "4. Objetivo 1 — Desarrollo resiliente al clima",
        "Preparás la evaluación de los criterios técnicos del Objetivo 1: "
        + ", ".join(OBJETIVO_1_CRITERIOS)
        + ".\n\n"
        + _CRITERIOS_INSTRUCTIONS,
        "",
        "table",
        _criterios_schema("Puntos clave — Objetivo 1", OBJETIVO_1_CRITERIOS),
        "tagged",
        ["salvaguardas-cesas", "documento-operacion", "ndc", "nap", "comunicacion-adaptacion"],
        "documents",
    ),
    (
        "5. Objetivo 2 — Desarrollo bajo en emisiones de GEI",
        "Preparás la evaluación de los criterios técnicos del Objetivo 2: "
        + ", ".join(OBJETIVO_2_CRITERIOS)
        + ".\n\n"
        + _CRITERIOS_INSTRUCTIONS,
        "",
        "table",
        _criterios_schema("Puntos clave — Objetivo 2", OBJETIVO_2_CRITERIOS),
        "tagged",
        ["documento-operacion", "ndc", "lts", "metodologia-caf"],
        "documents",
    ),
    (
        "6. Lo primero a mirar",
        "Con lo que señalaron los pasos anteriores, elegí los tres puntos que el "
        "ejecutivo debería mirar primero en esta operación, en orden de "
        "importancia.\n\n"
        "Sólo qué mirar y por qué: sin repetir el análisis ni adelantar "
        "determinaciones.",
        "Tres viñetas de una oración cada una. Máximo 60 palabras en total.",
        "text",
        {},
        "blueprint_only",
        [],
        "previous_steps",
    ),
]


def create_insumo_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")
    Organization = apps.get_model("user", "Organization")
    Project = apps.get_model("project", "Project")

    skill = Skill.objects.filter(slug=SLUG).first()
    if skill is None:
        iet = Skill.objects.filter(slug=IET_SLUG).first()
        skill = Skill.objects.create(
            owner=None,
            skill_type="copilot",
            name="Insumo para el ejecutivo — IDO",
            slug=SLUG,
            description=(
                "Ficha de dos páginas para el ejecutivo: lectura de la operación, "
                "contexto país, elegibilidad verde y puntos clave por criterio de "
                "alineación con París. No determina la alineación."
            ),
            allowed_contexts=["project"],
            system_prompt=SYSTEM_PROMPT,
            prompt_template="",
            # Misma configuración de modelo y recuperación que la IET: lee los
            # mismos documentos para la misma operación.
            model=(iet.model if iet else ""),
            temperature=(iet.temperature if iet else 0.3),
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
                    format_instructions=fmt,
                    position=position,
                    step_type="instruction",
                    output_mode=output_mode,
                    table_schema=schema,
                    output_validation="strict",
                    evidence_selection=selection,
                    evidence_tags=tags,
                    evidence_mode=evidence_mode,
                    approval_required=False,
                    comparative_enabled=False,
                )
                for position, (
                    title, instructions, fmt, output_mode, schema, selection, tags, evidence_mode
                ) in enumerate(STEPS, start=1)
            ]
        )

    org = Organization.objects.filter(slug=CAF_ORG_SLUG).first()
    if org is not None:
        org.enabled_skills.add(skill)
        org.default_project_skills.add(skill)

    iet = Skill.objects.filter(slug=IET_SLUG).first()
    if iet is not None:
        for project in Project.objects.filter(enabled_skills=iet):
            project.enabled_skills.add(skill)


def remove_insumo_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Skill.objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0032_skillexecution_stage"),
        ("user", "0007_seed_caf_default_project_skills"),
        ("project", "0017_stage_transitions_and_reports"),
    ]

    operations = [
        migrations.RunPython(create_insumo_workflow, remove_insumo_workflow),
    ]
