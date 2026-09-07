"""
Vocabulario inicial de etiquetas de evidencia.

Sale del mapeo del IET DATBC: son los tipos de documento que sus pasos
efectivamente piden. No pretende ser exhaustivo — el catálogo es ampliable
desde la aplicación, y esa es justamente la razón por la que sólo se siembran
los que hoy tienen un paso que los use.

``source_topics`` conecta cada etiqueta con las carpetas de la biblioteca CAF,
para que vincular un documento la proponga sin que nadie la tipee. Se guardan
por acrónimo: en la biblioteca las carpetas se llaman "NDCS: Contribuciones
Determinadas a Nivel Nacional" y el matching normaliza a "ndcs".
"""
from django.db import migrations

SEED = [
    {
        "slug": "ndc",
        "name": "NDC — Contribución Determinada a Nivel Nacional",
        "description": (
            "Compromiso climático nacional. Es la referencia de los criterios "
            "de consistencia (CT A3, CT M3)."
        ),
        "source_topics": ["ndcs", "ndc"],
        "position": 10,
    },
    {
        "slug": "nap",
        "name": "NAP — Plan Nacional de Adaptación",
        "description": "Instrumento nacional de adaptación al cambio climático.",
        "source_topics": ["naps", "nap"],
        "position": 20,
    },
    {
        "slug": "lts",
        "name": "LTS — Estrategia de Desarrollo a Largo Plazo",
        "description": (
            "Estrategia de largo plazo con bajas emisiones de GEI. Referencia "
            "del criterio de trayectorias (CT M4)."
        ),
        "source_topics": ["lts", "elp"],
        "position": 30,
    },
    {
        "slug": "comunicacion-adaptacion",
        "name": "AC — Comunicación de Adaptación",
        "description": "Comunicación de adaptación presentada ante la CMNUCC.",
        "source_topics": ["ac"],
        "position": 40,
    },
    {
        "slug": "nbsap",
        "name": "NBSAP — Estrategia Nacional de Biodiversidad",
        "description": "Estrategia y plan de acción nacional de biodiversidad.",
        "source_topics": ["nbsap", "nbsaps"],
        "position": 50,
    },
    {
        "slug": "pancd",
        "name": "PANCD — Plan de Acción contra la Desertificación",
        "description": "Plan de acción nacional de lucha contra la desertificación.",
        "source_topics": ["pancd"],
        "position": 60,
    },
    {
        "slug": "inventario-gei",
        "name": "Inventario de GEI / reporte de emisiones",
        "description": (
            "Inventarios nacionales, BUR, BTR y comunicaciones nacionales con "
            "datos de emisiones."
        ),
        "source_topics": ["inventario gei", "bur", "btr", "comunicacion nacional"],
        "position": 70,
    },
    {
        "slug": "metodologia-caf",
        "name": "Metodología y guías sectoriales CAF",
        "description": (
            "Manual de alineación, criterios de elegibilidad de financiamiento "
            "verde y guías técnicas sectoriales de la GACBP."
        ),
        "source_topics": ["metodologia caf", "guias sectoriales"],
        "position": 80,
    },
    {
        "slug": "salvaguardas-cesas",
        "name": "Salvaguardas / evaluación CESAS",
        "description": (
            "APRC, IAS, opiniones técnicas de CESAS y demás documentos de "
            "riesgo y salvaguardas de la operación."
        ),
        "source_topics": ["salvaguardas", "cesas", "aprc", "ias"],
        "position": 90,
    },
    {
        "slug": "documento-operacion",
        "name": "Documento de la operación",
        "description": (
            "Anexos y documentos propios de la operación distintos del "
            "principal: perfil, términos de referencia, estudios."
        ),
        "source_topics": [],
        "position": 100,
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
        ("document", "0015_evidence_tag"),
    ]

    operations = [
        migrations.RunPython(seed_tags, unseed_tags),
    ]
