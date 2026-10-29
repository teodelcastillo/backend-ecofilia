"""
Dos cambios en la IET, sin tocar los prompts de sus secciones:

**Documentos «Otros».** Los pasos que leen los documentos de la operación leen
también los etiquetados ``otros`` (``document/0022``), igual que el IBAT en
``0044``.

**La síntesis traduce los ocho CT.** Las reglas de los objetivos cortan el
recorrido donde un CT decide, y S.1 entendía que los CT siguientes «no
aplican»: los dejaba sin traducir. El Insumo IA muestra ahora los ocho, así que
S.1 traduce la conclusión de cada uno. El corte sigue valiendo para el
objetivo, que se calcula aparte.

Las reglas de ``0040`` no se reescriben (así volver a correr esa migración no
las duplica): se agrega una aclaración al final, que manda sobre el «no
aplican» de las reglas.
"""
from django.db import migrations

SLUGS = ("caf-iet-datbc-evaluacion-tecnica", "caf-iet-guiado-ejecutivo")
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
TAG = "otros"
S1_TITLE = "S.1. Síntesis por criterio"

OCHO_CT = (
    "\n\nAclaración sobre las reglas: que un CT decida el objetivo y los siguientes "
    "«no apliquen» vale sólo para calcular el objetivo. Traducí siempre los ocho CT "
    "(A1, A2, A3, M1, M2, M3, M4 y M5), también los que quedan después del CT que "
    "decide: el ejecutivo ve la conclusión de cada uno. Usá «No aplica» sólo en el "
    "CT A2, y sólo si su paso dice que no se analizaron medidas porque el riesgo es "
    "bajo o no corresponde."
)


def aplicar(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=SLUGS, evidence_selection="tagged"):
        tags = list(step.evidence_tags or [])
        if "documento-operacion" in tags and TAG not in tags:
            step.evidence_tags = tags + [TAG]
            step.save(update_fields=["evidence_tags"])

    s1 = SkillStep.objects.filter(skill__slug=IET_SLUG, title=S1_TITLE).first()
    if s1 is not None:
        text = s1.instructions or ""
        if OCHO_CT.strip() not in text:
            text = text + OCHO_CT
        if text != s1.instructions:
            s1.instructions = text
            s1.save(update_fields=["instructions"])


def revertir(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=SLUGS, evidence_selection="tagged"):
        tags = list(step.evidence_tags or [])
        if TAG in tags:
            step.evidence_tags = [tag for tag in tags if tag != TAG]
            step.save(update_fields=["evidence_tags"])

    s1 = SkillStep.objects.filter(skill__slug=IET_SLUG, title=S1_TITLE).first()
    if s1 is not None:
        text = (s1.instructions or "").replace(OCHO_CT, "")
        if text != s1.instructions:
            s1.instructions = text
            s1.save(update_fields=["instructions"])


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0045_execution_step_instructions"),
        ("document", "0022_seed_otros_tag"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
