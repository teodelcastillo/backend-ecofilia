"""
Etiqueta los documentos ya vinculados a operaciones existentes.

Sin esto, el mapeo del IET dejaría a los pasos por etiqueta sin evidencia en
todas las operaciones que ya están cargadas: los documentos están ahí, pero
nadie los etiquetó porque la etiqueta no existía cuando se vincularon.

Aplica la misma regla que se aplica al vincular —los topics de la biblioteca
proponen la etiqueta— y no toca los vínculos que ya tengan alguna, para que
una segunda corrida no pise decisiones humanas.
"""
from django.db import migrations


def _topic_keys(topics):
    keys = set()
    for raw in topics or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = raw.lower().strip()
        keys.add(value)
        head, sep, _ = value.partition(":")
        if sep and head.strip():
            keys.add(head.strip())
    return keys


def backfill_tags(apps, schema_editor):
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    ProjectDocument = apps.get_model("project", "ProjectDocument")

    tags = [t for t in EvidenceTag.objects.all() if t.source_topics]
    if not tags:
        return

    # `tags__isnull=True` son los vínculos sin ninguna etiqueta. Filtrarlos en
    # la base evita traer y descartar los que alguien ya etiquetó a mano.
    links = (
        ProjectDocument.objects
        .filter(tags__isnull=True)
        .select_related("document")
    )
    for link in links.iterator(chunk_size=500):
        keys = _topic_keys(getattr(link.document, "topics", None))
        if not keys:
            continue
        matching = [t for t in tags if keys.intersection(_topic_keys(t.source_topics))]
        if matching:
            link.tags.set(matching)


def noop(apps, schema_editor):
    """Sin reversa.

    Borrar las etiquetas al revertir no distinguiría las que puso este backfill
    de las que puso una persona después, y perder esas últimas es peor que
    dejar unas etiquetas de más.
    """


class Migration(migrations.Migration):
    dependencies = [
        ("project", "0009_projectdocument_tags"),
    ]

    operations = [
        migrations.RunPython(backfill_tags, noop),
    ]
