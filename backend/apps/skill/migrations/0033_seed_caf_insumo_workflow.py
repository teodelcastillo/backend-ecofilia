"""
Alta del workflow del insumo de Ecofilia para el ejecutivo.

Al entrar a IDO, lo primero que corre es este insumo: un análisis previo, más
corto y rápido que la IET, que el portal muestra como una herramienta web —con
gráficas, no como documento—. Cumple tres funciones a la vez:

1. **Le dice al ejecutivo qué mirar** antes de redactar su informe: lectura de
   la operación, contexto país y los **elementos clave para el análisis** (lo
   que en la IET es el B.5), que el ejecutivo tiene que leer antes de completar
   la alineación y el enverdecimiento.
2. **Es la fuente de las sugerencias** de Alineación (puntos clave y alertas por
   criterio) y de Enverdecimiento (componentes que podrían calificar y
   oportunidades). Así Ecofilia no se contradice entre pantallas.
3. **Guía a la IET de Ecofilia**, que corre después y lo recibe como contexto
   (ver ``apps.skill.insumo_guide``).

**No determina la alineación.** Por criterio señala qué mirar, alerta si ve una
posible brecha o una falta de información —que no son lo mismo— y declara lo
que falta. El veredicto de Ecofilia vive en su IET.

Los pasos de texto citan sus fuentes como la IET. La interfaz lee los pasos por
el número de su título ("1." … "8.") y las tablas por la clave de sus columnas:
los prompts se pueden ajustar desde el Studio, esa estructura conviene
mantenerla.

Se habilita en la organización CAF y en las operaciones que tienen la IET, igual
que hizo ``0028`` con el enverdecimiento.
"""
from django.db import migrations

SLUG = "caf-insumo-ejecutivo"
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
CAF_ORG_SLUG = "caf"

SYSTEM_PROMPT = (
    "Tu nombre es Ecofilia. Sos un analista técnico de la Dirección de Ambiente, "
    "Cambio Climático y Biodiversidad (DATBC) de CAF.\n\n"
    "Preparás el análisis previo de una operación en la etapa de Inventario de "
    "Operaciones (IDO): un insumo para el ejecutivo que va a evaluarla, y la guía "
    "con la que después se redacta el Informe de Evaluación Técnica (IET). Se lee "
    "en pantalla, por bloques: cada bloque tiene que entenderse solo.\n\n"
    "No determinás la alineación ni calificás los criterios: señalás lo relevante "
    "de los documentos, advertís lo que puede llevar a una mala lectura y "
    "declarás la información que falta. La decisión es del ejecutivo.\n\n"
    "Distinguí siempre una brecha de la operación de una falta de información en "
    "los documentos: no son lo mismo y llevan a determinaciones distintas.\n\n"
    "Escribís en español, en registro técnico y directo. Toda afirmación se apoya "
    "en los documentos de la operación y se cita; si un dato no está, decilo en "
    "lugar de suponerlo. Respetá el límite de extensión de cada paso."
)

OBJETIVO_VALUES = ["Adaptación", "Mitigación", "Biodiversidad y otros objetivos ambientales"]
ALERTAS = ["Brecha posible", "Falta de información", "Sin alertas"]

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

COMPONENTES_SCHEMA = {
    "name": "Componentes con potencial de financiamiento verde",
    "description": "Qué componentes podrían calificar a financiamiento verde de CAF.",
    "columns": [
        {"key": "componente", "label": "Componente", "type": "text", "required": True},
        {"key": "objetivo", "label": "Objetivo", "type": "enum", "required": True,
         "allowed_values": OBJETIVO_VALUES},
        {"key": "categoria", "label": "Categoría CAF", "type": "text",
         "prompt_hint": "Categoría de los Criterios y Actividades Elegibles de CAF."},
        {"key": "monto_usd", "label": "Monto USD", "type": "number",
         "prompt_hint": "Monto en dólares, sin separadores ni símbolo. Vacío si el documento no lo desagrega."},
        {"key": "confianza", "label": "Confianza", "type": "enum", "required": True,
         "allowed_values": ["Alta", "Media", "Baja"]},
        {"key": "justificacion", "label": "Justificación", "type": "text", "required": True,
         "prompt_hint": "Por qué podría calificar, en no más de 40 palabras."},
    ],
}

OPORTUNIDADES_SCHEMA = {
    "name": "Oportunidades de enverdecimiento",
    "description": "Qué agregar o ajustar para que la operación califique más.",
    "columns": [
        {"key": "oportunidad", "label": "Oportunidad", "type": "text", "required": True},
        {"key": "objetivo", "label": "Objetivo", "type": "enum", "required": True,
         "allowed_values": OBJETIVO_VALUES},
        {"key": "meta_pais", "label": "Meta del país que apalanca", "type": "text", "required": True},
        {"key": "monto_usd", "label": "Monto USD potencial", "type": "number",
         "prompt_hint": "Monto que pasaría a contar como verde. Sin separadores ni símbolo."},
        {"key": "esfuerzo", "label": "Qué haría falta", "type": "text", "required": True,
         "prompt_hint": "Acción concreta y de quién depende, en una oración."},
    ],
}


def _criterios_schema(name: str, criterios: list[str]) -> dict:
    return {
        "name": name,
        "description": "Qué mirar en cada criterio técnico, sin determinarlo.",
        "columns": [
            {"key": "criterio", "label": "Criterio", "type": "enum", "required": True,
             "allowed_values": criterios},
            {"key": "puntos_clave", "label": "Puntos clave y advertencias", "type": "text",
             "required": True,
             "prompt_hint": (
                 "Qué mirar en los documentos y qué advertir antes de determinar el "
                 "criterio, con la fuente. Máximo 80 palabras. Sin sugerir el resultado."
             )},
            {"key": "informacion_faltante", "label": "Información faltante", "type": "text",
             "required": True,
             "prompt_hint": "Qué falta para evaluarlo. Máximo 30 palabras. 'Nada relevante' si no falta nada."},
            {"key": "alerta", "label": "Alerta", "type": "enum", "required": True,
             "allowed_values": ALERTAS,
             "prompt_hint": (
                 "'Brecha posible' si los documentos muestran algo que podría no cumplir el "
                 "criterio; 'Falta de información' si no alcanzan para evaluarlo; 'Sin "
                 "alertas' si no hay nada que advertir."
             )},
        ],
    }


_CRITERIOS_INSTRUCTIONS = (
    "Armá una fila por criterio, en el orden de la lista y sin saltear ninguno.\n\n"
    "En «puntos clave» poné lo que el ejecutivo tiene que mirar en los documentos "
    "para evaluar ese criterio en esta operación y lo que tiene que advertir antes "
    "de determinarlo, nombrando el documento del que sale. En «información "
    "faltante», lo que los documentos no dicen y haría falta.\n\n"
    "En «alerta», distinguí: una posible brecha de la operación no es lo mismo que "
    "una falta de información en los documentos. No determines el criterio ni "
    "sugieras su resultado."
)

# (título, instrucciones, formato, modo, esquema, selección, etiquetas, evidencia)
STEPS = [
    (
        "1. Lectura de la operación",
        "Describí qué financia la operación: instrumento, componentes principales con "
        "sus montos si están, ubicación y beneficiarios, según el documento principal.\n\n"
        "Cerrá con lo que el documento deja sin definir y que importa para la "
        "evaluación climática (por ejemplo, la ubicación de las obras o la tecnología).",
        "Uno o dos párrafos de prosa. Máximo 180 palabras.",
        "text", {}, "blueprint_only", [], "documents",
    ),
    (
        "2. Contexto país",
        "Resumí el contexto climático y de biodiversidad del país que importa para esta "
        "operación: los compromisos de la NDC y del Plan Nacional de Adaptación para su "
        "sector, las amenazas climáticas principales para la zona o el sector, el "
        "perfil de emisiones del sector y si el país tiene Estrategia de Largo Plazo.\n\n"
        "Citá el instrumento de cada dato. Dejá afuera lo que no se conecta con la "
        "operación.",
        "Prosa breve o hasta cinco viñetas. Máximo 200 palabras.",
        "text", {}, "tagged", ["ndc", "nap", "lts", "comunicacion-adaptacion", "nbsap"], "documents",
    ),
    (
        "3. Elementos clave para el análisis de la operación",
        "Conectá el contexto del país con esta operación en particular: a qué metas de "
        "la NDC, el NAP, la NBSAP y el PANCD contribuye, con cuáles tiene tensión y "
        "cuáles no le aplican.\n\n"
        "Cerrá con los aspectos que la evaluación de alineación y de enverdecimiento "
        "tiene que mirar con más detalle. Es lo que el ejecutivo lee antes de "
        "determinar: tiene que poder orientarlo por sí solo.",
        "Prosa o viñetas. Máximo 220 palabras.",
        "text", {}, "tagged", ["ndc", "nap", "nbsap", "pancd"], "both",
    ),
    (
        "4. Componentes con potencial de financiamiento verde",
        "Identificá los componentes o subcomponentes que podrían calificar a "
        "financiamiento verde según los Criterios y Actividades Elegibles de CAF. Una "
        "fila por componente.\n\n"
        "En Monto USD poné sólo lo que el expediente permita atribuirle; si no lo "
        "desagrega, dejalo vacío y decilo en la justificación. En Confianza, 'Alta' es "
        "que el documento lo dice, 'Media' que se desprende de lo que dice, 'Baja' que "
        "es plausible pero no está sostenido. Si ningún componente califica, devolvé la "
        "tabla vacía.",
        "", "table", COMPONENTES_SCHEMA, "tagged", ["documento-operacion", "metodologia-caf"], "documents",
    ),
    (
        "5. Oportunidades de enverdecimiento",
        "Proponé qué se podría agregar o ajustar en la operación para que califique "
        "más a financiamiento verde, atando cada oportunidad a una meta concreta del "
        "país (NDC, NAP o LTS). Una fila por oportunidad, de la que más mueve a la que "
        "menos. Si no hay oportunidades razonables, devolvé la tabla vacía.",
        "", "table", OPORTUNIDADES_SCHEMA, "tagged", ["ndc", "nap", "lts", "metodologia-caf"], "both",
    ),
    (
        "6. Objetivo 1 — Desarrollo resiliente al clima",
        "Preparás la evaluación de los criterios técnicos del Objetivo 1: "
        + ", ".join(OBJETIVO_1_CRITERIOS) + ".\n\n" + _CRITERIOS_INSTRUCTIONS,
        "", "table", _criterios_schema("Puntos clave — Objetivo 1", OBJETIVO_1_CRITERIOS),
        "tagged", ["salvaguardas-cesas", "documento-operacion", "ndc", "nap", "comunicacion-adaptacion"],
        "documents",
    ),
    (
        "7. Objetivo 2 — Desarrollo bajo en emisiones de GEI",
        "Preparás la evaluación de los criterios técnicos del Objetivo 2: "
        + ", ".join(OBJETIVO_2_CRITERIOS) + ".\n\n" + _CRITERIOS_INSTRUCTIONS,
        "", "table", _criterios_schema("Puntos clave — Objetivo 2", OBJETIVO_2_CRITERIOS),
        "tagged", ["documento-operacion", "ndc", "lts", "metodologia-caf"], "documents",
    ),
    (
        "8. Lo primero a mirar",
        "Con lo que señalaron los pasos anteriores, elegí los tres puntos que el "
        "ejecutivo debería mirar primero en esta operación, en orden de importancia.\n\n"
        "Sólo qué mirar y por qué: sin repetir el análisis ni adelantar "
        "determinaciones.",
        "Tres viñetas de una oración cada una. Máximo 70 palabras en total.",
        "text", {}, "blueprint_only", [], "previous_steps",
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
            name="Insumo de Ecofilia — análisis previo IDO",
            slug=SLUG,
            description=(
                "Análisis previo de la operación: lectura, contexto país, elementos clave, "
                "componentes y oportunidades de financiamiento verde, y puntos clave por "
                "criterio de alineación con París. Guía a la IET y no determina la alineación."
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
