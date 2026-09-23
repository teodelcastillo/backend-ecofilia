"""
Lleva las etiquetas de evidencia de los vínculos a los documentos.

Hasta acá la etiqueta vivía en ``ProjectDocument.tags`` y se proponía una sola
vez, al vincular. Ahora vive en el documento y la operación la hereda. Esta
migración arma ese estado a partir de lo que hay, sin perder nada que haya
decidido una persona:

1. **Documentos con temas reconocibles** reciben la etiqueta que sugieren esos
   temas, ahora comparando sin acentos (antes "metodología caf" no matcheaba
   "metodologia caf" y la etiqueta nunca se proponía).
2. **Documentos sin etiqueta cuyos vínculos coinciden** —todas las operaciones
   que los etiquetaron les pusieron lo mismo— reciben esa etiqueta. Es el caso
   de lo que un ejecutivo marcó a mano en su operación.
3. **Cada vínculo** hereda si sus etiquetas eran las del documento o estaban
   vacías. Si eran otras, las conserva como decisión de la operación
   (``tags_overridden``).
4. **Instrumentos país**: un vínculo vacío que ahora heredaría "ndc" en una
   operación donde ya hay otra NDC es una versión vieja que la sincronización
   había desetiquetado. Se la deja sin esa etiqueta, para que el paso que pide
   "la NDC" no lea dos.

La lógica de matching está copiada acá y no importada: una migración tiene
que dar el mismo resultado aunque el código de la aplicación cambie después.
"""
import re
import unicodedata
from collections import defaultdict

from django.db import migrations

COUNTRY_TAGS = {"ndc", "nap", "lts", "comunicacion-adaptacion"}
_SPLIT = re.compile(r"[,;/()_\-\s]+")


def _norm(value):
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.lower().split())


def _doc_keys(topics):
    keys = set()
    for raw in topics or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = _norm(raw)
        keys.add(value)
        head, sep, _ = value.partition(":")
        if sep and head.strip():
            keys.add(head.strip())
        for frag in _SPLIT.split(value):
            if len(frag) >= 3:
                keys.add(frag)
    return keys


def _matches(source_topics, keys):
    for raw in source_topics or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        wanted = _norm(raw)
        if wanted in keys:
            return True
        if " " in wanted and any(
            re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", key) for key in keys
        ):
            return True
    return False


def forwards(apps, schema_editor):
    Document = apps.get_model("document", "Document")
    EvidenceTag = apps.get_model("document", "EvidenceTag")
    ProjectDocument = apps.get_model("project", "ProjectDocument")

    catalog = list(EvidenceTag.objects.all())
    by_id = {t.id: t for t in catalog}
    with_topics = [t for t in catalog if t.source_topics]

    # ── 1. Temas → etiqueta del documento ────────────────────────────────
    doc_tags: dict[int, set[int]] = {}
    for doc in Document.objects.exclude(topics=[]).only("id", "topics").iterator(chunk_size=500):
        keys = _doc_keys(doc.topics)
        if not keys:
            continue
        matched = {t.id for t in with_topics if _matches(t.source_topics, keys)}
        if matched:
            doc_tags[doc.id] = matched

    # Etiquetas actuales de cada vínculo.
    link_tags: dict[int, set[int]] = defaultdict(set)
    Through = ProjectDocument.tags.through
    for link_id, tag_id in Through.objects.values_list("projectdocument_id", "evidencetag_id"):
        link_tags[link_id].add(tag_id)

    links = list(ProjectDocument.objects.values_list("id", "project_id", "document_id"))

    # ── 2. Vínculos que coinciden → etiqueta del documento ───────────────
    sets_by_doc: dict[int, set[frozenset]] = defaultdict(set)
    for link_id, _project_id, doc_id in links:
        if link_tags.get(link_id):
            sets_by_doc[doc_id].add(frozenset(link_tags[link_id]))
    for doc_id, sets in sets_by_doc.items():
        if doc_id not in doc_tags and len(sets) == 1:
            doc_tags[doc_id] = set(next(iter(sets)))

    for doc_id, tag_ids in doc_tags.items():
        Document.objects.get(pk=doc_id).evidence_tags.set(list(tag_ids))

    # ── 3. Cada vínculo: hereda o conserva lo suyo ───────────────────────
    effective: dict[int, set[int]] = {}
    inherited_from_empty: set[int] = set()
    overridden: set[int] = set()
    for link_id, _project_id, doc_id in links:
        own = link_tags.get(link_id, set())
        from_doc = doc_tags.get(doc_id, set())
        if own and own != from_doc:
            overridden.add(link_id)
            effective[link_id] = own
        else:
            effective[link_id] = from_doc
            if not own and from_doc:
                inherited_from_empty.add(link_id)

    # ── 4. Una sola versión vigente de cada instrumento por operación ────
    country_ids = {t.id for t in catalog if t.slug in COUNTRY_TAGS}
    links_by_project: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for link_id, project_id, doc_id in links:
        links_by_project[project_id].append((link_id, doc_id))
    for project_links in links_by_project.values():
        for tag_id in country_ids:
            holders = [lid for lid, _ in project_links if tag_id in effective[lid]]
            if len(holders) < 2:
                continue
            # Los que ya lo tenían antes de esta migración son los vigentes;
            # los que lo ganaron recién son versiones que se habían sacado.
            if not any(lid not in inherited_from_empty for lid in holders):
                continue
            for lid in holders:
                if lid in inherited_from_empty:
                    effective[lid] = effective[lid] - {tag_id}
                    overridden.add(lid)

    for link_id in overridden:
        link = ProjectDocument.objects.get(pk=link_id)
        link.tags_overridden = True
        link.save(update_fields=["tags_overridden"])
        link.tags.set([by_id[t] for t in effective[link_id] if t in by_id])
    # Los que heredan no guardan etiquetas propias: se leen del documento.
    inheriting = [lid for lid, _, _ in links if lid not in overridden]
    Through.objects.filter(projectdocument_id__in=inheriting).delete()


def backwards(apps, schema_editor):
    """Vuelve a dejar en cada vínculo las etiquetas que tenía efectivamente."""
    ProjectDocument = apps.get_model("project", "ProjectDocument")
    for link in ProjectDocument.objects.filter(tags_overridden=False).select_related("document"):
        link.tags.set(list(link.document.evidence_tags.all()))


class Migration(migrations.Migration):
    dependencies = [
        ("document", "0018_evidence_tags_on_document"),
        ("project", "0011_evidence_tags_on_document"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
