"""
Los pasos de síntesis de la IET ven completas las secciones que integran.

Desde la 0037 las determinaciones de cada objetivo (D.2 y D.3) y la Sección E
no releen el expediente: integran lo que ya analizaron los pasos anteriores.
Pero con la historia en modo automático un paso ve completas sólo las dos
secciones anteriores y el resto resumido a 1.200 caracteres: la determinación
del Objetivo 1 veía el CT A1 resumido, y E.1 casi toda la Sección D. Ahora cada
uno elige sus secciones, como la síntesis ``S.1.``:

- D.2 Determinación — los CT A1, A2 y A3.
- D.3 Determinación — los CT M1 a M5.
- E.1 — toda la Sección D.
- E.3 — B.5, C.1, las dos determinaciones y E.1.

Las secciones se buscan por título y no por posición: la IET de producción
puede haberse ajustado desde el Studio.
"""
from django.db import migrations

IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"


def _positions(steps, predicate) -> list[int]:
    return [step.position for step in steps if predicate((step.title or "").strip())]


def _es_determinacion(title: str) -> bool:
    return "Determinación y recomendaciones" in title


def _secciones(steps) -> dict[str, list[int]]:
    return {
        "D.2-det": _positions(steps, lambda t: t.startswith("D.2. CT")),
        "D.3-det": _positions(steps, lambda t: t.startswith("D.3. CT")),
        "E.1": _positions(steps, lambda t: t.startswith("D.")),
        "E.3": _positions(
            steps,
            lambda t: t.startswith(("B.5.", "C.1.", "E.1.")) or _es_determinacion(t),
        ),
    }


def _clave(title: str) -> str | None:
    if _es_determinacion(title):
        return "D.2-det" if title.startswith("D.2.") else "D.3-det" if title.startswith("D.3.") else None
    if title.startswith("E.1."):
        return "E.1"
    if title.startswith("E.3."):
        return "E.3"
    return None


def aplicar(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    steps = list(SkillStep.objects.filter(skill__slug=IET_SLUG).order_by("position"))
    secciones = _secciones(steps)
    for step in steps:
        clave = _clave((step.title or "").strip())
        if clave is None or step.evidence_mode != "previous_steps":
            continue
        positions = [p for p in secciones[clave] if p < step.position]
        if not positions:
            continue
        step.history_mode = "selected"
        step.history_positions = positions
        step.save(update_fields=["history_mode", "history_positions"])


def revertir(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug=IET_SLUG, history_mode="selected"):
        if _clave((step.title or "").strip()) is not None:
            step.history_mode = "auto"
            step.history_positions = []
            step.save(update_fields=["history_mode", "history_positions"])


class Migration(migrations.Migration):
    dependencies = [
        ("skill", "0037_caf_tope_documentos_y_sintesis"),
    ]

    operations = [
        migrations.RunPython(aplicar, revertir),
    ]
