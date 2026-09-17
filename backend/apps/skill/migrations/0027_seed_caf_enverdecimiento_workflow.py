"""
Alta del workflow de enverdecimiento de una operación.

Responde dos preguntas que hoy nadie contesta con evidencia: qué parte de la
operación ya califica como financiamiento verde, y qué haría falta para que
califique más, atado a las metas del país.

Tres pasos, y cada uno existe por una razón distinta:

  V.1 sale en tabla porque la pantalla suma montos. El porcentaje verde lo
      calcula el frontend dividiendo por el monto CAF de la operación — pedirle
      la división al modelo es la forma más fácil de publicar un número
      inventado.
  V.2 lee la NDC, el NAP y la LTS por etiqueta de evidencia. Son los
      documentos que la operación ya trae vinculados por su país, así que la
      meta que se cita es la del país y no una genérica.
  V.3 vuelve a ser tabla: cada oportunidad es una fila con su monto potencial
      y la meta que apalanca, para poder ordenarlas por lo que mueven.

Los pasos no repiten el contexto de la operación (país, monto, objetivo,
componentes, documento principal): eso lo inyecta el motor — ver
``apps.skill.operation_context``.
"""
from django.db import migrations

SLUG = "caf-enverdecimiento-operacion"
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"

SYSTEM_PROMPT = (
    "Tu nombre es Ecofilia. Sos un analista de la Dirección de Ambiente, Cambio "
    "Climático y Biodiversidad (DATBC) de CAF, especializado en elegibilidad de "
    "financiamiento verde.\n\n"
    "Trabajás sobre una operación concreta y sobre los documentos vinculados a "
    "ella. Escribís en español, en registro técnico, y toda afirmación se apoya "
    "en esos documentos.\n\n"
    "Cuando un dato no esté en los documentos, decilo explícitamente en lugar de "
    "completarlo con conocimiento general. Un monto estimado se declara como "
    "estimación y se explica de dónde sale; nunca se presenta como dato del "
    "expediente."
)

OBJETIVO_VALUES = ["Adaptación", "Mitigación", "Biodiversidad y otros objetivos ambientales"]

ELEGIBLE_SCHEMA = {
    "name": "Componentes elegibles",
    "description": "Componentes de la operación que ya califican a financiamiento verde.",
    "columns": [
        {"key": "componente", "label": "Componente", "type": "text", "required": True},
        {
            "key": "objetivo",
            "label": "Objetivo",
            "type": "enum",
            "required": True,
            "allowed_values": OBJETIVO_VALUES,
        },
        {"key": "categoria", "label": "Categoría CAF", "type": "text"},
        {
            "key": "monto_usd",
            "label": "Monto USD",
            "type": "number",
            "prompt_hint": (
                "Monto en dólares, sin separadores ni símbolo. Vacío si el "
                "expediente no permite atribuirle un monto."
            ),
        },
        {
            "key": "confianza",
            "label": "Confianza",
            "type": "enum",
            "allowed_values": ["Alta", "Media", "Baja"],
        },
        {"key": "justificacion", "label": "Justificación", "type": "text", "required": True},
    ],
}

OPORTUNIDAD_SCHEMA = {
    "name": "Oportunidades de enverdecimiento",
    "description": "Qué agregar o negociar para que la operación califique más.",
    "columns": [
        {"key": "oportunidad", "label": "Oportunidad", "type": "text", "required": True},
        {
            "key": "objetivo",
            "label": "Objetivo",
            "type": "enum",
            "required": True,
            "allowed_values": OBJETIVO_VALUES,
        },
        {
            "key": "meta_pais",
            "label": "Meta del país que apalanca",
            "type": "text",
            "required": True,
        },
        {
            "key": "monto_usd",
            "label": "Monto USD potencial",
            "type": "number",
            "prompt_hint": (
                "Monto que pasaría a contar como verde si la oportunidad se "
                "concreta. Sin separadores ni símbolo."
            ),
        },
        {
            "key": "esfuerzo",
            "label": "Qué haría falta",
            "type": "text",
            "required": True,
            "prompt_hint": "Acción concreta y de quién depende, en una oración.",
        },
    ],
}

STEPS = [
    {
        "title": "V.1. Componentes que ya califican a financiamiento verde",
        "instructions": (
            "Identificá los componentes o subcomponentes de esta operación que ya "
            "califican a financiamiento verde según los Criterios y Actividades "
            "Elegibles de Financiamiento Verde de CAF.\n\n"
            "Una fila por componente. En Monto USD poné sólo lo que el expediente "
            "permita atribuirle; si el documento no desagrega el monto, dejalo "
            "vacío y explicalo en la justificación en vez de estimarlo. En "
            "Confianza, 'Alta' es que el documento lo dice, 'Media' que se "
            "desprende de lo que dice, 'Baja' que es plausible pero no está "
            "sostenido.\n\n"
            "Si ningún componente califica, devolvé la tabla vacía. Una operación "
            "sin componentes verdes es un resultado legítimo, y forzar filas para "
            "no entregar una tabla vacía es peor que el vacío."
        ),
        "evidence_selection": "blueprint_only",
        "evidence_tags": [],
        "output_mode": "table",
        "table_schema": ELEGIBLE_SCHEMA,
    },
    {
        "title": "V.2. Metas del país que la operación podría apalancar",
        "instructions": (
            "Leé los instrumentos de política climática del país vinculados a la "
            "operación (NDC, Plan Nacional de Adaptación, Estrategia de Largo "
            "Plazo) y listá las metas que se relacionan con el sector y la "
            "actividad de esta operación.\n\n"
            "Para cada meta: qué compromete, a qué horizonte, y por qué esta "
            "operación puede contribuir a ella o tensionarla. Citá el instrumento "
            "y la parte de donde sale.\n\n"
            "Dejá afuera las metas que no tengan relación con el sector de la "
            "operación, por relevantes que sean para el país."
        ),
        "evidence_selection": "tagged",
        "evidence_tags": ["ndc", "nap", "lts"],
        "output_mode": "text",
        "table_schema": {},
    },
    {
        "title": "V.3. Oportunidades para aumentar el financiamiento verde",
        "instructions": (
            "Proponé qué podría agregarse o negociarse con el cliente para que una "
            "porción mayor de la operación califique a financiamiento verde.\n\n"
            "Una fila por oportunidad, ordenadas de mayor a menor monto potencial. "
            "Cada una tiene que ser concreta y hacer pie en dos cosas: los "
            "componentes que ya tiene la operación (paso V.1) y una meta del país "
            "del paso V.2, que va en la columna correspondiente.\n\n"
            "En Monto USD potencial estimá lo que pasaría a contar como verde, y "
            "aclarás en 'Qué haría falta' de qué depende. No repitas lo que ya "
            "califica: acá va únicamente lo que hoy no cuenta y podría contar."
        ),
        "evidence_selection": "all",
        "evidence_tags": [],
        "output_mode": "table",
        "table_schema": OPORTUNIDAD_SCHEMA,
    },
]


def create_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")

    if Skill.objects.filter(slug=SLUG).exists():
        return

    # Hereda la configuración del workflow del IET: corre sobre las mismas
    # operaciones y con los mismos documentos, así que no hay razón para que
    # recupere distinto.
    base = Skill.objects.filter(slug=IET_SLUG).first()

    skill = Skill.objects.create(
        owner=None,
        skill_type="copilot",
        name="Enverdecimiento — Potencial verde de la operación",
        slug=SLUG,
        description=(
            "Identifica qué parte de la operación ya califica a financiamiento "
            "verde y qué haría falta para que califique más, atado a las metas "
            "climáticas del país."
        ),
        allowed_contexts=["project"],
        system_prompt=(base.system_prompt if base else SYSTEM_PROMPT),
        prompt_template="",
        model=(base.model if base else "gpt-4o-mini"),
        temperature=(base.temperature if base else 0.3),
        retrieval_strategy=(base.retrieval_strategy if base else "global"),
        k_per_doc=(base.k_per_doc if base else 2),
        total_limit=(base.total_limit if base else 12),
    )

    for position, step in enumerate(STEPS, start=1):
        SkillStep.objects.create(
            skill=skill,
            title=step["title"],
            instructions=step["instructions"],
            position=position,
            step_type="instruction",
            evidence_selection=step["evidence_selection"],
            evidence_tags=step["evidence_tags"],
            output_mode=step["output_mode"],
            table_schema=step["table_schema"],
            # Las tablas nacen estrictas: una fila que no respeta el esquema
            # rompe la suma de montos, que es de lo que vive esta pantalla.
            output_validation="strict" if step["output_mode"] == "table" else "lenient",
            approval_required=False,
        )


def remove_workflow(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    Skill.objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0026_skillexecution_promoted_at_executionsectionedit_and_more"),
    ]

    operations = [
        migrations.RunPython(create_workflow, remove_workflow),
    ]
