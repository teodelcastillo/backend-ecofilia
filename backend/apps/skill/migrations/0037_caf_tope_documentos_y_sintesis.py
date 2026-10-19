"""
Corridas de CAF más livianas.

Cada paso de la IET leía documentos enteros hasta llenar la ventana de un
millón de tokens, con un conjunto distinto por paso (según sus etiquetas), así
que la caché casi no se reutilizaba. Era caro —por encima de 200k tokens se
cobra precio de contexto largo— y una IET llegó a exceder la ventana.

- **Tope de documentos por paso** (``Skill.max_document_tokens``): 300.000
  tokens para el insumo, que es el análisis rápido. La IET y el guiado quedan
  sin tope: son el informe, y su calidad manda. El documento principal sigue
  entero siempre; los anexos que no entran se leen por fragmentos. Se ajusta
  por workflow desde el admin o el Studio, después de medir corridas reales.
- **Pasos de síntesis sin documentos**: las determinaciones de cada objetivo
  (D.2 y D.3) y la Sección E de la IET integran lo que ya analizaron los pasos
  anteriores, igual que la síntesis ``S.1.``. Leen esas secciones, no el
  expediente de nuevo.
"""
from django.db import migrations, models

IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
CAPPED_SLUGS = ("caf-insumo-ejecutivo",)
MAX_DOCUMENT_TOKENS = 300_000


def _es_sintesis(title: str) -> bool:
    title = (title or "").strip()
    return "Determinación y recomendaciones" in title or title.startswith(("E.1.", "E.3."))


def aplicar(apps, schema_editor):
    Skill = apps.get_model("skill", "Skill")
    SkillStep = apps.get_model("skill", "SkillStep")
    Skill.objects.filter(slug__in=CAPPED_SLUGS, max_document_tokens__isnull=True).update(
        max_document_tokens=MAX_DOCUMENT_TOKENS
    )
    for step in SkillStep.objects.filter(skill__slug=IET_SLUG, evidence_mode="both"):
        if _es_sintesis(step.title):
            step.evidence_mode = "previous_steps"
            step.save(update_fields=["evidence_mode"])


def revertir(apps, schema_editor):
    SkillStep = apps.get_model("skill", "SkillStep")
    for step in SkillStep.objects.filter(skill__slug=IET_SLUG, evidence_mode="previous_steps"):
        if _es_sintesis(step.title):
            step.evidence_mode = "both"
            step.save(update_fields=["evidence_mode"])


class Migration(migrations.Migration):

    dependencies = [
        ("skill", "0036_seed_caf_lectura_informe_workflow"),
    ]

    operations = [
        migrations.AddField(
            model_name="skill",
            name="max_document_tokens",
            field=models.PositiveIntegerField(
                blank=True,
                help_text=(
                    "Tope de tokens de documentos por paso. Vacío: lo que entre en la "
                    "ventana del modelo. El documento principal siempre va entero."
                ),
                null=True,
            ),
        ),
        migrations.RunPython(aplicar, revertir),
    ]
