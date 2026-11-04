"""
Enverdecimiento coherente entre el informe y el guiado, Misión de Evaluación y
componentes verdes en la lectura de un informe hecho por fuera.

- **S.2 por actividad.** C.1 nombra, por componente, todas las actividades del
  catálogo con las que califica (la principal primero). S.2 hacía una fila por
  componente y se quedaba con una: la ficha y el guiado mostraban menos
  actividades, y peor identificadas, que el informe. Ahora va una fila por
  actividad elegible, y el monto de un componente que C.1 no desagrega se pone
  una sola vez, para que el % verde no lo cuente dos veces.
- **Misión de Evaluación.** Los pasos de los workflows de CAF que leen los
  documentos de la operación (los que piden «otros») también la piden.
- **Lectura del informe subido.** Suma «4. Componentes con potencial de
  financiamiento verde», con las mismas columnas que S.2: un IET hecho por fuera
  (histórico o subido por el ejecutivo) entra al Resumen y al tablero con su
  % verde, igual que uno trabajado en Ecofilia.

Los pasos del informe original (B, D, E) no se tocan. Reversible.
"""
import copy

from django.db import migrations

from apps.skill.caf_catalogo_verde import como_referencia

S2_TITLE = "S.2. Componentes con potencial de financiamiento verde"
LECTURA_SLUG = "caf-lectura-informe-ejecutivo"
LECTURA_VERDE_TITLE = "4. Componentes con potencial de financiamiento verde"
CAF_ORG_SLUG = "caf"
TAG = "mision-evaluacion"
ANTES_DE = "otros"

S2_VIEJO = (
    "Pasá a una tabla los componentes que la Sección C.1 presenta como elegibles o "
    "con potencial de financiamiento verde. No vuelvas a analizar el expediente: una "
    "fila por componente, con el objetivo (Adaptación, Mitigación, o Biodiversidad y "
    "otros objetivos ambientales), la categoría CAF y el monto cuando C.1 los dice, y "
    "un fundamento corto tomado de C.1."
)
S2_NUEVO = (
    "Pasá a una tabla las actividades que la Sección C.1 presenta como elegibles o "
    "con potencial de financiamiento verde. No vuelvas a analizar el expediente: lo "
    "que dice C.1 va completo, sin dejar afuera ninguna actividad que presente como "
    "elegible o potencialmente elegible.\n\n"
    "Una fila por actividad elegible de cada componente. Si C.1 dice que un componente "
    "califica por más de una actividad, o por más de un objetivo, va una fila por cada "
    "una, con el mismo nombre de componente. En cada fila: el objetivo (Adaptación, "
    "Mitigación, o Biodiversidad y otros objetivos ambientales), la categoría CAF, la "
    "actividad y un fundamento corto tomado de C.1.\n\n"
    "En «monto_usd» poné el monto que C.1 atribuye a esa actividad. Si C.1 da el monto "
    "del componente sin desagregarlo por actividad, ponelo una sola vez, en la primera "
    "fila de ese componente, y dejá vacío el de las demás: el monto de un componente "
    "nunca se repite, porque las filas se suman para calcular el porcentaje verde de "
    "la operación."
)

LECTURA_VERDE_INSTRUCTIONS = (
    "Registrá las actividades que el informe presenta como elegibles o con potencial "
    "de financiamiento verde (en un IET, la Sección C). Una fila por actividad elegible "
    "de cada componente: si el informe dice que un componente califica por más de una "
    "actividad, va una fila por cada una, con el mismo nombre de componente.\n\n"
    "Registrá lo que el informe dice, no lo que vos concluirías: el objetivo, la "
    "actividad del catálogo de CAF con la que califica, el monto y un fundamento corto "
    "tomado del informe. En «monto_usd» poné el monto en dólares que el informe "
    "atribuye a esa actividad; si da el monto del componente sin desagregarlo, ponelo "
    "una sola vez, en la primera fila de ese componente. Si el informe no identifica "
    "actividades elegibles, devolvé la tabla vacía.\n\n"
    "## Catálogo de actividades elegibles de CAF\n\n"
    "En «codigo_actividad» poné el código de cuatro niveles de la actividad (por "
    "ejemplo B.1.1.1), sólo de esta lista; dejalo vacío si ninguna corresponde. En "
    "«actividad_elegible», el nombre de esa actividad tal como figura en el catálogo.\n\n"
)

LECTURA_VERDE_SCHEMA = {
    "name": "Componentes con potencial de financiamiento verde",
    "description": "Las actividades que el informe presenta como elegibles o con potencial.",
    "columns": [
        {"key": "componente", "type": "text", "label": "Componente", "required": True},
        {"key": "codigo_actividad", "type": "text", "label": "Código de actividad CAF", "required": False,
         "prompt_hint": "Un solo código de cuatro niveles (p. ej. B.1.1.1), sin el nombre. Vacío si ninguna corresponde."},
        {"key": "actividad_elegible", "type": "text", "label": "Actividad elegible CAF", "required": False,
         "prompt_hint": "El nombre de la actividad del catálogo de CAF. Vacío si ninguna corresponde."},
        {"key": "objetivo", "type": "enum", "label": "Objetivo", "required": True,
         "allowed_values": ["Adaptación", "Mitigación", "Biodiversidad y otros objetivos ambientales"]},
        {"key": "monto_usd", "type": "number", "label": "Monto USD", "required": False,
         "prompt_hint": "Monto en dólares, sin separadores ni símbolo. Vacío si el informe no lo dice."},
        {"key": "fundamento", "type": "text", "label": "Fundamento", "required": True,
         "prompt_hint": "Por qué califica según el informe, en no más de 35 palabras."},
    ],
}


def _caf_steps(SkillStep):
    return SkillStep.objects.filter(skill__organization__slug=CAF_ORG_SLUG)


def aplicar(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")

    for step in SkillStep.objects.filter(title=S2_TITLE):
        if S2_VIEJO in step.instructions:
            step.instructions = step.instructions.replace(S2_VIEJO, S2_NUEVO)
            schema = copy.deepcopy(step.table_schema or {})
            if schema:
                schema["description"] = "Las actividades que la Sección C presenta como elegibles o con potencial."
                step.table_schema = schema
            step.save(update_fields=["instructions", "table_schema"])

    for step in _caf_steps(SkillStep):
        tags = list(step.evidence_tags or [])
        if ANTES_DE in tags and TAG not in tags:
            tags.insert(tags.index(ANTES_DE), TAG)
            step.evidence_tags = tags
            step.save(update_fields=["evidence_tags"])

    lectura = Skill.objects.filter(slug=LECTURA_SLUG).first()
    if lectura is not None and not lectura.steps.filter(title=LECTURA_VERDE_TITLE).exists():
        last = lectura.steps.order_by("-position").first()
        SkillStep.objects.create(
            skill=lectura,
            title=LECTURA_VERDE_TITLE,
            instructions=LECTURA_VERDE_INSTRUCTIONS + como_referencia(),
            position=(last.position + 1) if last else 1,
            step_type="instruction",
            output_mode="table",
            table_schema=LECTURA_VERDE_SCHEMA,
            output_validation="strict",
            evidence_selection="all",
            evidence_tags=[],
            evidence_mode="documents",
            approval_required=False,
            comparative_enabled=False,
        )


def revertir(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")

    for step in SkillStep.objects.filter(title=S2_TITLE):
        if S2_NUEVO in step.instructions:
            step.instructions = step.instructions.replace(S2_NUEVO, S2_VIEJO)
            schema = copy.deepcopy(step.table_schema or {})
            if schema:
                schema["description"] = "Los componentes que la Sección C presenta como elegibles o con potencial."
                step.table_schema = schema
            step.save(update_fields=["instructions", "table_schema"])

    for step in _caf_steps(SkillStep):
        tags = list(step.evidence_tags or [])
        if TAG in tags:
            step.evidence_tags = [t for t in tags if t != TAG]
            step.save(update_fields=["evidence_tags"])

    lectura = Skill.objects.filter(slug=LECTURA_SLUG).first()
    if lectura is not None:
        lectura.steps.filter(title=LECTURA_VERDE_TITLE).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0051_defaults_en_la_base"),
        ("document", "0027_mision_de_evaluacion_caf"),
    ]

    operations = [migrations.RunPython(aplicar, revertir)]
