"""
«Otros» como documento complementario de la operación.

Para lo que no es IAS, IBAT, análisis de riesgos, matriz ni diseños (un estudio
de factibilidad, una nota técnica del ejecutor). El ejecutivo dice en una línea
qué es (la descripción del documento). Los pasos de la IET que leen los
documentos de la operación lo suman en ``skill/0046``.
"""
from django.db import migrations

ROW = {
    "slug": "otros",
    "name": "Otros",
    "description": "Otro documento de la operación: estudios, notas técnicas, informes del ejecutor.",
    "source_topics": ["otros"],
    "position": 108,
}


def seed_tag(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    EvidenceTag.objects.update_or_create(
        slug=ROW["slug"],
        defaults={
            "name": ROW["name"],
            "description": ROW["description"],
            "source_topics": ROW["source_topics"],
            "position": ROW["position"],
            "is_seed": True,
        },
    )


def unseed_tag(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    EvidenceTag.objects.filter(slug=ROW["slug"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0021_seed_ley_y_reporte_nacional"),
    ]

    operations = [
        migrations.RunPython(seed_tag, unseed_tag),
    ]
