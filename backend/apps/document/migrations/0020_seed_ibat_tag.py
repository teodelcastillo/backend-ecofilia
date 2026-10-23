"""
El IBAT como documento complementario de la operación.

El reporte del IBAT (Integrated Biodiversity Assessment Tool) dice qué áreas
protegidas y especies amenazadas hay cerca de la operación. Se carga en el alta
junto con el IAS y los demás documentos de la operación, todos opcionales. Los
pasos de la IET que leen los documentos de la operación lo suman en
``skill/0044``.
"""
from django.db import migrations

ROW = {
    "slug": "ibat",
    "name": "IBAT",
    "description": "Reporte del IBAT: áreas protegidas y especies amenazadas cerca de la operación.",
    "source_topics": ["ibat"],
    "position": 105,
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
        ("document", "0019_seed_operation_evidence_tags"),
    ]

    operations = [
        migrations.RunPython(seed_tag, unseed_tag),
    ]
