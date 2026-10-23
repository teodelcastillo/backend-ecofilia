"""
Los pasos de la IET que leen los documentos de la operación leen también el IBAT.

``document/0020`` siembra la etiqueta ``ibat``. Igual que en ``0042``, se suma
donde un paso ya pide «Documento de la operación»; los prompts no se tocan. Se
aplica a la IET y al guiado, que copia su configuración de evidencia.
"""
from django.db import migrations

SLUGS = ("caf-iet-datbc-evaluacion-tecnica", "caf-iet-guiado-ejecutivo")
TAG = "ibat"


def aplicar(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=SLUGS, evidence_selection="tagged"):
        tags = list(step.evidence_tags or [])
        if "documento-operacion" in tags and TAG not in tags:
            step.evidence_tags = tags + [TAG]
            step.save(update_fields=["evidence_tags"])


def revertir(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=SLUGS, evidence_selection="tagged"):
        tags = list(step.evidence_tags or [])
        if TAG in tags:
            step.evidence_tags = [tag for tag in tags if tag != TAG]
            step.save(update_fields=["evidence_tags"])


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0043_s2_actividad_elegible"),
        ("document", "0020_seed_ibat_tag"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
