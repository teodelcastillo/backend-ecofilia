"""
Etiquetas para el documento principal y los documentos de la operación.

El alta de una operación pide marcar el principal como IDO o DEC (según la
etapa) y clasificar los documentos propios de la operación por tipo: el IAS, el
análisis de riesgos, la matriz de resultados y los diseños. Hasta ahora todo
eso cabía en «Documento de la operación» y «Salvaguardas / evaluación CESAS»;
los pasos de la IET que leen esas dos etiquetas suman las nuevas en
``skill/0042``, así que un documento marcado con una etiqueta fina sigue
llegando a los pasos que lo necesitan.

Los grupos con los que el portal las muestra (principal, operación, contexto)
salen del slug (ver ``apps.document.evidence_tag_groups``), no de la base.
"""
from django.db import migrations

SEED = [
    {
        "slug": "ido",
        "name": "Documento IDO",
        "description": "El documento principal de la operación en la etapa de IDO.",
        "source_topics": [],
        "position": 1,
    },
    {
        "slug": "dec",
        "name": "Documento DEC",
        "description": "El documento principal de la operación en la etapa de DEC.",
        "source_topics": [],
        "position": 2,
    },
    {
        "slug": "ias",
        "name": "IAS",
        "description": "Informe ambiental y social de la operación.",
        "source_topics": ["ias"],
        "position": 101,
    },
    {
        "slug": "analisis-riesgos",
        "name": "Análisis de riesgos",
        "description": "Análisis de riesgos climáticos de la operación (APRC u otro).",
        "source_topics": ["analisis de riesgos"],
        "position": 102,
    },
    {
        "slug": "matriz-resultados",
        "name": "Matriz de Resultados",
        "description": "Matriz de resultados e indicadores de la operación.",
        "source_topics": ["matriz de resultados"],
        "position": 103,
    },
    {
        "slug": "disenos-proyecto",
        "name": "Diseños de proyectos",
        "description": "Diseños, estudios de ingeniería y planos de la operación.",
        "source_topics": ["disenos de proyecto", "disenos del proyecto"],
        "position": 104,
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
        ("document", "0018_evidence_tags_on_document"),
    ]

    operations = [
        migrations.RunPython(seed_tags, unseed_tags),
    ]
