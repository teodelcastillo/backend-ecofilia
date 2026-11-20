"""
«IET IDO» como documento de la operación (CAF).

El Informe de Evaluación Técnica de la etapa IDO. El informe de DEC lo lee para
analizar cómo evolucionó la operación (paso «A.4. Antecedentes…») y para
confirmar las actividades elegibles. Cuando el IDO se hizo en Ecofilia, el
informe emitido se vincula solo a la operación al lanzar el DEC
(``apps.project.services.stage_inheritance``); si se hizo por fuera, se sube con
esta etiqueta.

Si la etiqueta ya existía (creada a mano en producción), se completa sin
duplicarla y la reversa no la borra. Si no existe la institución ``caf``, no
hace nada.
"""
from django.db import migrations

CAF_SLUG = "caf"
ROW = {
    "slug": "iet-datbc-ido",
    "name": "IET IDO",
    "description": "Informe de Evaluación Técnica de la DATBC de la etapa IDO de la operación.",
    "source_topics": ["iet ido", "informe de evaluacion tecnica ido", "iet-datbc ido"],
    "position": 107,
}


def seed(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    Organization = apps.get_model("user", "Organization")
    caf = Organization.objects.filter(slug=CAF_SLUG).first()
    if caf is None:
        return
    if EvidenceTag.objects.filter(slug=ROW["slug"], organization=caf).exists():
        return
    EvidenceTag.objects.create(
        slug=ROW["slug"],
        organization=caf,
        name=ROW["name"],
        description=ROW["description"],
        source_topics=ROW["source_topics"],
        position=ROW["position"],
        is_seed=True,
    )


def unseed(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    EvidenceTag.objects.filter(
        slug=ROW["slug"], organization__slug=CAF_SLUG, is_seed=True
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0027_mision_de_evaluacion_caf"),
        ("user", "0016_caf_dec_y_mision"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
