"""
Los pasos de la IET leen las etiquetas finas de los documentos de la operación.

``document/0019`` siembra las etiquetas con las que el alta clasifica los
documentos propios de la operación (IAS, análisis de riesgos, matriz de
resultados, diseños). Un documento marcado así tiene que llegar a los mismos
pasos que antes leían «Documento de la operación» o «Salvaguardas / CESAS», así
que la configuración de esos pasos suma las nuevas; los prompts no se tocan.

- Pasos que piden ``documento-operacion`` → también las cuatro de operación.
- Pasos que piden ``salvaguardas-cesas`` (CT A1, CT A2) → también ``ias`` y
  ``analisis-riesgos``.

Se aplica a la IET y al guiado, que copia la configuración de evidencia de los
pasos C, D y E de la IET (``0035``): si no, el guiado reescribiría el CT A1 sin
ver el IAS que la IET sí leyó.
"""
from django.db import migrations

SLUGS = ("caf-iet-datbc-evaluacion-tecnica", "caf-iet-guiado-ejecutivo")

OPERACION = ["ias", "analisis-riesgos", "matriz-resultados", "disenos-proyecto"]
SALVAGUARDAS = ["ias", "analisis-riesgos"]

EXTRA_BY_TAG = {
    "documento-operacion": OPERACION,
    "salvaguardas-cesas": SALVAGUARDAS,
}


def _extras(tags: list[str]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        for extra in EXTRA_BY_TAG.get(tag, []):
            if extra not in out:
                out.append(extra)
    return out


def aplicar(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=SLUGS, evidence_selection="tagged"):
        tags = list(step.evidence_tags or [])
        missing = [extra for extra in _extras(tags) if extra not in tags]
        if missing:
            step.evidence_tags = tags + missing
            step.save(update_fields=["evidence_tags"])


def revertir(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug__in=SLUGS, evidence_selection="tagged"):
        tags = list(step.evidence_tags or [])
        extras = set(_extras(tags))
        kept = [tag for tag in tags if tag not in extras]
        if kept != tags:
            step.evidence_tags = kept
            step.save(update_fields=["evidence_tags"])


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0041_insumo_deja_de_ser_agente"),
        ("document", "0019_seed_operation_evidence_tags"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
