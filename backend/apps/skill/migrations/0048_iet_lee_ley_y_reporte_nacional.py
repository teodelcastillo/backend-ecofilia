"""
Los pasos de contexto país de la IET leen también la ley de cambio climático y
el reporte nacional (BTR, BUR o Comunicación Nacional).

Son los pasos que leen la NDC junto con otros instrumentos (NAP, LTS, AdCom,
NBSAP). Los criterios que piden un solo instrumento —el CT M3 lee la NDC y nada
más— no cambian.

Son instrumentos del contexto país que se vinculan solos a la operación
(``country_documents``). Como en ``0042`` y ``0044``, se suman a la
configuración de evidencia de los pasos; los prompts no se tocan. Se aplica a la
IET y al guiado, que copia su configuración de evidencia.
"""
from django.db import migrations

SLUGS = ("caf-iet-datbc-evaluacion-tecnica", "caf-iet-guiado-ejecutivo")
NUEVAS = ["ley-cambio-climatico", "reporte-nacional"]
OTROS_INSTRUMENTOS = ["nap", "lts", "comunicacion-adaptacion", "nbsap"]


def aplicar(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=SLUGS, evidence_selection="tagged"):
        tags = list(step.evidence_tags or [])
        if "ndc" not in tags or not any(t in tags for t in OTROS_INSTRUMENTOS):
            continue
        faltan = [t for t in NUEVAS if t not in tags]
        if faltan:
            step.evidence_tags = tags + faltan
            step.save(update_fields=["evidence_tags"])


def revertir(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=SLUGS, evidence_selection="tagged"):
        tags = list(step.evidence_tags or [])
        if any(t in tags for t in NUEVAS):
            step.evidence_tags = [t for t in tags if t not in NUEVAS]
            step.save(update_fields=["evidence_tags"])


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0047_iet_catalogo_actividades_caf"),
        ("document", "0021_seed_ley_y_reporte_nacional"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
