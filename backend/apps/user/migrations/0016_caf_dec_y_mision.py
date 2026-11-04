"""
Configuración de CAF: el informe de DEC y la Misión de Evaluación.

- **DEC tiene su propio informe de Ecofilia** (``stages[].report``): el workflow
  ``dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion``, que se lanza
  al entrar a DEC. Sólo si la skill existe en esta base (en producción la armó
  el staff en el Studio); si no, la etapa queda como estaba.
- **Misión de Evaluación** entra al grupo de documentos de la operación.

Si no existe la institución ``caf`` o no tiene configuración, no hace nada.
Reversible.
"""
import copy

from django.db import migrations

CAF_SLUG = "caf"
DEC_STAGE = "dec"
DEC_REPORT = "dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion"
TAG = "mision-evaluacion"
GROUP = "operacion"


def aplicar(apps, schema_editor):
    Organization = apps.get_model("user", "Organization")
    Skill = apps.get_model("skill", "Skill")
    caf = Organization.objects.filter(slug=CAF_SLUG).first()
    if caf is None or not caf.config:
        return
    config = copy.deepcopy(caf.config)
    if Skill.objects.filter(slug=DEC_REPORT).exists():
        for stage in config.get("stages") or []:
            if stage.get("slug") == DEC_STAGE:
                stage["report"] = DEC_REPORT
                launches = list(stage.get("launches") or [])
                if DEC_REPORT not in launches:
                    launches.append(DEC_REPORT)
                stage["launches"] = launches
    for group in config.get("evidence_tag_groups") or []:
        if group.get("slug") == GROUP and TAG not in (group.get("tags") or []):
            tags = list(group.get("tags") or [])
            # Antes de «otros», que va siempre al final.
            at = tags.index("otros") if "otros" in tags else len(tags)
            tags.insert(at, TAG)
            group["tags"] = tags
    caf.config = config
    caf.save(update_fields=["config"])


def revertir(apps, schema_editor):
    Organization = apps.get_model("user", "Organization")
    caf = Organization.objects.filter(slug=CAF_SLUG).first()
    if caf is None or not caf.config:
        return
    config = copy.deepcopy(caf.config)
    for stage in config.get("stages") or []:
        if stage.get("slug") == DEC_STAGE and stage.get("report") == DEC_REPORT:
            stage.pop("report", None)
            stage["launches"] = [s for s in stage.get("launches") or [] if s != DEC_REPORT]
    for group in config.get("evidence_tag_groups") or []:
        group["tags"] = [t for t in group.get("tags") or [] if t != TAG]
    caf.config = config
    caf.save(update_fields=["config"])


class Migration(migrations.Migration):
    dependencies = [
        ("user", "0015_cifrar_secretos_mfa"),
        ("document", "0027_mision_de_evaluacion_caf"),
        ("skill", "0051_defaults_en_la_base"),
    ]

    operations = [migrations.RunPython(aplicar, revertir)]
