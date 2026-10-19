"""
El informe guiado reescribe la Sección E cuando algo difiere.

El guiado decide sección por sección si el criterio del ejecutivo cambia algo
y, si no, responde «SIN CAMBIOS». Pero E.1 y E.3 resumen todos los criterios:
si difería sólo un CT, el modelo podía reescribir ese CT y su determinación y
dejar la Sección E con la conclusión de Ecofilia, contradiciendo al resto del
informe. Ahora esas secciones se reescriben siempre que algún criterio o el
enverdecimiento estén marcados como distintos.
"""
from django.db import migrations

GUIADO_SLUG = "caf-iet-guiado-ejecutivo"
REGLA = (
    "\n\nEsta sección resume todos los criterios. Si en los parámetros de la "
    "corrida algún criterio está marcado «DIFIERE», o el enverdecimiento «DIFIERE "
    "del insumo», reescribila completa con el criterio del ejecutivo: en ese caso "
    "no respondas «SIN CAMBIOS», aunque el texto de esta sección no nombre el "
    "criterio que cambió."
)


def _es_resumen(title: str) -> bool:
    return (title or "").strip().startswith(("E.1.", "E.3."))


def aplicar(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug=GUIADO_SLUG):
        if _es_resumen(step.title) and REGLA.strip() not in (step.instructions or ""):
            step.instructions = (step.instructions or "") + REGLA
            step.save(update_fields=["instructions"])


def revertir(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug=GUIADO_SLUG):
        if _es_resumen(step.title) and REGLA in (step.instructions or ""):
            step.instructions = step.instructions.replace(REGLA, "")
            step.save(update_fields=["instructions"])


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0038_iet_sintesis_lee_sus_secciones"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
