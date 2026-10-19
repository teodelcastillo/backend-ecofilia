"""
S.2 nombra la actividad elegible de CAF con la que califica cada componente.

La ficha del Insumo identifica, por componente de la operación, la actividad de
la lista de Criterios y Actividades Elegibles de Financiamiento Verde de CAF con
la que califica: es lo que C.1 dice y lo que el ejecutivo tiene que ver. La
«categoría» de la 0040 no alcanza, porque agrupa varias actividades.

Sólo cambia el paso oculto S.2 de la IET (la tabla que lee la ficha): una
columna nueva, ``actividad_elegible``, y una línea en sus instrucciones. Los
pasos del informe no se tocan.
"""
from django.db import migrations

IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
S2_TITLE = "S.2. Componentes con potencial de financiamiento verde"

ACTIVIDAD_COLUMN = {
    "key": "actividad_elegible",
    "label": "Actividad elegible CAF",
    "type": "text",
    "required": False,
    "prompt_hint": (
        "La actividad de la lista de Criterios y Actividades Elegibles de Financiamiento "
        "Verde de CAF con la que el componente califica, con el nombre que le da C.1. "
        "Vacío si C.1 no la nombra."
    ),
}

INSTRUCCION = (
    "\n\nPara cada componente, en «actividad_elegible» nombrá la actividad de la lista "
    "de Criterios y Actividades Elegibles de CAF con la que califica, tal como la "
    "nombra C.1: es lo que el ejecutivo ve en la ficha de la operación."
)


def _s2(apps):
    SkillStep = apps.get_model("skill", "SkillStep")
    return SkillStep.objects.filter(skill__slug=IET_SLUG, title=S2_TITLE).first()


def aplicar(apps, schema_editor):
    step = _s2(apps)
    if step is None:
        return
    schema = dict(step.table_schema or {})
    columns = [dict(c) for c in schema.get("columns") or []]
    if not any(c.get("key") == "actividad_elegible" for c in columns):
        # Después del componente: es lo que lo hace elegible.
        index = next((i + 1 for i, c in enumerate(columns) if c.get("key") == "componente"), len(columns))
        columns.insert(index, dict(ACTIVIDAD_COLUMN))
    schema["columns"] = columns
    step.table_schema = schema
    if INSTRUCCION.strip() not in (step.instructions or ""):
        step.instructions = (step.instructions or "") + INSTRUCCION
    step.save(update_fields=["table_schema", "instructions"])


def revertir(apps, schema_editor):
    step = _s2(apps)
    if step is None:
        return
    schema = dict(step.table_schema or {})
    schema["columns"] = [dict(c) for c in schema.get("columns") or [] if c.get("key") != "actividad_elegible"]
    step.table_schema = schema
    step.instructions = (step.instructions or "").replace(INSTRUCCION, "")
    step.save(update_fields=["table_schema", "instructions"])


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0042_iet_lee_etiquetas_de_operacion"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
