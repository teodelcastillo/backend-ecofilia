"""
Dos instrumentos más del contexto país que se vinculan solos a la operación.

- ``ley-cambio-climatico``: la ley nacional (o marco) de cambio climático.
- ``reporte-nacional``: el reporte del país ante la CMNUCC —BTR, BUR o
  Comunicación Nacional—. Traen información muy parecida, así que es una sola
  etiqueta: con la regla de «el más nuevo gana» de ``country_documents`` se
  vincula sólo el más actual de los tres.
"""
from django.db import migrations

SEED = [
    {
        "slug": "ley-cambio-climatico",
        "name": "Ley Nacional de Cambio Climático",
        "description": "La ley nacional o ley marco de cambio climático del país.",
        "source_topics": ["ley de cambio climatico", "ley marco de cambio climatico"],
        "position": 26,
    },
    {
        "slug": "reporte-nacional",
        "name": "BTR / BUR / Comunicación Nacional",
        "description": (
            "El reporte del país ante la CMNUCC: Informe Bienal de Transparencia (BTR), "
            "Informe Bienal de Actualización (BUR) o Comunicación Nacional. Se usa el más reciente."
        ),
        "source_topics": [
            "btr",
            "bur",
            "comunicacion nacional",
            "informe bienal de transparencia",
            "informe bienal de actualizacion",
        ],
        "position": 27,
    },
]


def seed_tags(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    for row in SEED:
        EvidenceTag.objects.update_or_create(
            slug=row["slug"],
            defaults={
                "name": row["name"],
                "description": row["description"],
                "source_topics": row["source_topics"],
                "position": row["position"],
                "is_seed": True,
            },
        )


def unseed_tags(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    EvidenceTag.objects.filter(slug__in=[row["slug"] for row in SEED]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0020_seed_ibat_tag"),
    ]

    operations = [
        migrations.RunPython(seed_tags, unseed_tags),
    ]
