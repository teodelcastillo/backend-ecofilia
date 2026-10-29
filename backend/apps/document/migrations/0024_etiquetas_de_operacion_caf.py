"""
Las etiquetas de operación de CAF pasan a ser de CAF (fase 0: aislamiento).

Desde ``0023`` una etiqueta sin institución es global y una con institución
la ve sólo esa institución. Las de contexto de país (NDC, NAP, LTS, …) siguen
globales: son instrumentos públicos de cada país. Las que describen piezas de
una operación de CAF (IDO, DEC, IAS, IBAT, …) son de CAF.

Si la base no tiene la institución CAF, no se toca nada y todas quedan
globales. Reversible: al volver, las de CAF vuelven a ser globales.
"""
from django.db import migrations

CAF_SLUG = "caf"
CAF_OPERATION_TAGS = (
    "ido",
    "dec",
    "ias",
    "ibat",
    "analisis-riesgos",
    "matriz-resultados",
    "disenos-proyecto",
    "otros",
    "documento-operacion",
)


def asignar(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    Organization = apps.get_model("user", "Organization")
    db = schema_editor.connection.alias if schema_editor else "default"

    caf = Organization.objects.using(db).filter(slug=CAF_SLUG).first()
    if caf is None:
        return
    EvidenceTag.objects.using(db).filter(
        slug__in=CAF_OPERATION_TAGS, organization__isnull=True
    ).update(organization=caf)


def revertir(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    Organization = apps.get_model("user", "Organization")
    db = schema_editor.connection.alias if schema_editor else "default"

    caf = Organization.objects.using(db).filter(slug=CAF_SLUG).first()
    if caf is None:
        return
    # Una global con el mismo slug creada después impediría volver: el slug
    # global es único. Esas se dejan como están.
    globales = EvidenceTag.objects.using(db).filter(
        organization__isnull=True, slug__in=CAF_OPERATION_TAGS
    ).values_list("slug", flat=True)
    EvidenceTag.objects.using(db).filter(
        slug__in=CAF_OPERATION_TAGS, organization=caf
    ).exclude(slug__in=list(globales)).update(organization=None)


class Migration(migrations.Migration):

    dependencies = [
        ("document", "0023_evidence_tag_organization"),
        ("user", "0007_seed_caf_default_project_skills"),
    ]

    operations = [
        migrations.RunPython(asignar, revertir),
    ]
