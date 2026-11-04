"""
«Misión de Evaluación» como documento complementario de la operación (CAF).

El informe de la misión que visita la operación: lo que vio el equipo en el
terreno y lo que acordó con el ejecutor. Es de CAF (fase 0); la configuración
de CAF la suma al grupo de documentos de la operación en ``user/0016`` y los
pasos que leen esos documentos la piden en ``skill/0052``.

Si no existe la institución ``caf``, no hace nada. Reversible.
"""
from django.db import migrations

CAF_SLUG = "caf"
ROW = {
    "slug": "mision-evaluacion",
    "name": "Misión de Evaluación",
    "description": "Informe de la misión de evaluación: lo observado en terreno y lo acordado con el ejecutor.",
    "source_topics": ["mision de evaluacion", "misión de evaluación"],
    "position": 106,
}


def seed(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    Organization = apps.get_model("user", "Organization")
    caf = Organization.objects.filter(slug=CAF_SLUG).first()
    if caf is None:
        return
    EvidenceTag.objects.update_or_create(
        slug=ROW["slug"],
        organization=caf,
        defaults={
            "name": ROW["name"],
            "description": ROW["description"],
            "source_topics": ROW["source_topics"],
            "position": ROW["position"],
            "is_seed": True,
        },
    )


def unseed(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    EvidenceTag.objects.filter(slug=ROW["slug"], organization__slug=CAF_SLUG).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0026_metodologia_y_salvaguardas_de_caf"),
        ("user", "0015_cifrar_secretos_mfa"),
    ]

    operations = [migrations.RunPython(seed, unseed)]
