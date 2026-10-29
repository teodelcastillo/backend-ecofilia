"""
``metodologia-caf`` y ``salvaguardas-cesas`` también son de CAF (fase 0).

``0024`` pasó a CAF las etiquetas de operación y dejó globales las de país.
Estas dos no estaban en ninguna de las dos listas y quedaron globales, así que
cualquier institución veía «Metodología y guías sectoriales CAF» y las
salvaguardas de CAF en su catálogo. Son de CAF.

Migración aparte y no un cambio a ``0024``: las bases que ya la aplicaron (las
locales del front, por ejemplo) la reciben con el próximo ``migrate``.

Si no existe la institución ``caf``, no hace nada. Reversible.
"""
from django.db import migrations

CAF_SLUG = "caf"
CAF_TAGS = ("metodologia-caf", "salvaguardas-cesas")


def asignar(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    Organization = apps.get_model("user", "Organization")
    db = schema_editor.connection.alias if schema_editor else "default"

    caf = Organization.objects.using(db).filter(slug=CAF_SLUG).first()
    if caf is None:
        return
    EvidenceTag.objects.using(db).filter(slug__in=CAF_TAGS, organization__isnull=True).update(
        organization=caf
    )


def revertir(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    Organization = apps.get_model("user", "Organization")
    db = schema_editor.connection.alias if schema_editor else "default"

    caf = Organization.objects.using(db).filter(slug=CAF_SLUG).first()
    if caf is None:
        return
    # Una global con el mismo slug creada después impediría volver: se deja.
    globales = list(
        EvidenceTag.objects.using(db)
        .filter(organization__isnull=True, slug__in=CAF_TAGS)
        .values_list("slug", flat=True)
    )
    EvidenceTag.objects.using(db).filter(slug__in=CAF_TAGS, organization=caf).exclude(
        slug__in=globales
    ).update(organization=None)


class Migration(migrations.Migration):

    dependencies = [
        ("document", "0024_etiquetas_de_operacion_caf"),
    ]

    operations = [
        migrations.RunPython(asignar, revertir),
    ]
