"""
El catálogo de actividades elegibles de CAF como referencia fija del análisis.

Hasta acá C.1 nombraba la categoría o la subcategoría (B.1.1) y S.2 copiaba lo
que dijera C.1. Ahora:

- **C.1** (en la IET y en el guiado) recibe el catálogo completo y, por cada
  componente, nombra la actividad de cuarto nivel (B.1.1.1) con su código, sólo
  de la lista.
- **S.2** suma la columna ``codigo_actividad``: el código de cuarto nivel.
  Es texto y no lista cerrada a propósito: S.2 valida en estricto, y un código
  fuera de la lista haría fallar el análisis entero. El portal descarta el que
  no esté en el catálogo. También recibe el catálogo, para resolverlo aunque
  C.1 sea de antes (completar los criterios rehace S.2 sobre el C.1 que ya
  estaba).

Los pasos del informe original (B, D, E) no se tocan.
"""
from django.db import migrations

from apps.skill.caf_catalogo_verde import como_referencia

IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
GUIADO_SLUG = "caf-iet-guiado-ejecutivo"
C1_PREFIX = "C.1."
S2_TITLE = "S.2. Componentes con potencial de financiamiento verde"

MARCA = "\n\n## Catálogo de actividades elegibles de CAF"


def _bloque(rol: str) -> str:
    return (
        f"{MARCA}\n\n{rol}\n\n"
        "El catálogo tiene cuatro niveles: objetivo (A, B, M), categoría (A.1), "
        "subcategoría (A.1.1) y actividad (A.1.1.1). El código que corresponde a un "
        "componente es siempre el de la actividad, de cuatro niveles, y sólo puede "
        "ser uno de esta lista: no inventes códigos ni uses los de categoría o "
        "subcategoría.\n\n"
        f"{como_referencia()}"
    )


C1_ROL = (
    "Para cada componente que califica o podría calificar, nombrá la actividad del "
    "catálogo con la que califica, con su código de cuatro niveles y su nombre (por "
    "ejemplo «B.1.1.1 Restauración y gestión integral de cuencas, subcuencas "
    "hidrográficas»). Si califica por más de una, nombrá la principal primero. Si "
    "ninguna actividad corresponde, decilo: el componente queda como potencial."
)
S2_ROL = (
    "En «codigo_actividad» poné el código de cuatro niveles de la actividad con la que "
    "C.1 dice que califica el componente. Si C.1 sólo nombra la categoría o la "
    "subcategoría, elegí la actividad de esa subcategoría que mejor describe el "
    "componente. Dejalo vacío si ninguna corresponde. En «actividad_elegible» poné el "
    "nombre de esa actividad tal como figura en el catálogo."
)

CODIGO_COLUMN = {
    "key": "codigo_actividad",
    "label": "Código de actividad CAF",
    "type": "text",
    "required": False,
    "prompt_hint": "Un solo código de cuatro niveles (p. ej. B.1.1.1) de la actividad del catálogo, sin el nombre. Vacío si ninguna corresponde.",
}


def _con_bloque(step, rol):
    text = step.instructions or ""
    if MARCA in text:
        text = text[: text.index(MARCA)]
    step.instructions = text + _bloque(rol)


def _sin_bloque(step):
    text = step.instructions or ""
    if MARCA in text:
        step.instructions = text[: text.index(MARCA)]


def aplicar(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=(IET_SLUG, GUIADO_SLUG), title__startswith=C1_PREFIX):
        _con_bloque(step, C1_ROL)
        step.save(update_fields=["instructions"])

    s2 = SkillStep.objects.filter(skill__slug=IET_SLUG, title=S2_TITLE).first()
    if s2 is None:
        return
    _con_bloque(s2, S2_ROL)
    schema = dict(s2.table_schema or {})
    columns = [dict(c) for c in schema.get("columns") or [] if c.get("key") != "codigo_actividad"]
    posicion = next((i + 1 for i, c in enumerate(columns) if c.get("key") == "componente"), 1)
    columns.insert(posicion, dict(CODIGO_COLUMN))
    for column in columns:
        if column.get("key") == "actividad_elegible":
            column["prompt_hint"] = "El nombre de la actividad del catálogo de CAF, tal como figura en él. Vacío si ninguna corresponde."
    schema["columns"] = columns
    s2.table_schema = schema
    s2.save(update_fields=["instructions", "table_schema"])


def revertir(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=(IET_SLUG, GUIADO_SLUG), title__startswith=C1_PREFIX):
        _sin_bloque(step)
        step.save(update_fields=["instructions"])
    s2 = SkillStep.objects.filter(skill__slug=IET_SLUG, title=S2_TITLE).first()
    if s2 is None:
        return
    _sin_bloque(s2)
    schema = dict(s2.table_schema or {})
    schema["columns"] = [c for c in schema.get("columns") or [] if c.get("key") != "codigo_actividad"]
    s2.table_schema = schema
    s2.save(update_fields=["instructions", "table_schema"])


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0046_iet_otros_y_ocho_criterios"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
