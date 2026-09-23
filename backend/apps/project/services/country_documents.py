"""
Instrumentos país (NDC, NAP, LTS, AC): asignación automática por país.

Son documentos que un país presenta ante la CMNUCC y versiona en el tiempo —
hay una NDC de 2016 y otra de 2021 para el mismo país. Para una operación de
ese país, lo que importa es la vigente: el agente que lee "la NDC" tiene que
leer la última, no una desactualizada por dos ciclos. El resto del catálogo
de etiquetas (salvaguardas, metodología CAF, documento de la operación) no
tiene este patrón de "una vigente por país" y se deja afuera a propósito:
vincularlas sigue siendo una decisión manual de quien arma la operación.
"""
from __future__ import annotations

from django.db.models import F

from apps.document.models import Document, EvidenceTag
from apps.document.services import accessible_library_documents, normalize_topic
from apps.project.models import Project, ProjectDocument
from apps.project.services.evidence_tags import effective_tags, override_tags

# Ampliable: cualquier etiqueta de la biblioteca donde "un documento por
# país, el más nuevo gana" tenga sentido puede sumarse acá (ver el catálogo
# sembrado en apps/document/migrations/0016_seed_evidence_tags.py — nbsap y
# pancd son candidatos razonables si en algún momento se pide extenderlo).
COUNTRY_INSTRUMENT_TAG_SLUGS = ["ndc", "nap", "lts", "comunicacion-adaptacion"]


def country_instrument_documents(
    user, region: str, tag_slugs=COUNTRY_INSTRUMENT_TAG_SLUGS
) -> dict[str, list[Document]]:
    """
    Documentos de ``region`` accesibles para ``user`` etiquetados con cada
    instrumento, del más nuevo al más viejo.

    "Más nuevo" es el año del documento y, a igualdad o sin año, el de carga:
    una NDC de 2016 subida tarde no puede desplazar a la de 2021.

    El país se compara sin mayúsculas ni acentos: "Peru" cargado a mano en la
    biblioteca es el mismo país que "Perú" en el formulario de la operación.
    Filtrado por accesibilidad (propios + públicos + compartidos, igual que el
    resto de la biblioteca) para no auto-vincular un documento privado de otro
    usuario a una operación ajena.
    """
    result: dict[str, list[Document]] = {slug: [] for slug in tag_slugs}
    wanted_region = normalize_topic(region or "")
    if not wanted_region:
        return result
    candidates = (
        accessible_library_documents(user)
        .filter(evidence_tags__slug__in=tag_slugs, region__isnull=False)
        .prefetch_related("evidence_tags")
        .order_by(F("year").desc(nulls_last=True), "-created_at")
        .distinct()
    )
    for doc in candidates:
        if normalize_topic(doc.region or "") != wanted_region:
            continue
        for tag in doc.evidence_tags.all():
            if tag.slug in result:
                result[tag.slug].append(doc)
    return result


def sync_country_instrument_documents(project: Project) -> dict[str, Document]:
    """
    Vincula a ``project`` el documento vigente de cada instrumento país según
    ``context_notes["pais"]``.

    El vínculo nuevo hereda la etiqueta del documento, así que no hay nada que
    marcar. Lo que sí hace falta es que una versión vieja que ya estaba
    vinculada deje de contar como "la" NDC — sin desvincularla, porque puede
    seguir citada por otro motivo —; si no, el paso que pide la NDC leería las
    dos. A ese vínculo viejo se le fija, en la operación, el resto de sus
    etiquetas sin la del instrumento.

    Una decisión tomada en la operación no se pisa: si alguien le sacó la
    etiqueta NDC al documento vigente, repetir la sincronización no se la
    vuelve a poner.

    Se llama al crear o guardar una operación con país cargado, y repetirlo no
    tiene costo: sin novedades en la biblioteca es un no-op.
    """
    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    region = notes.get("pais")
    if not isinstance(region, str) or not region.strip():
        return {}

    by_tag = country_instrument_documents(project.owner, region.strip())
    tags_by_slug = {t.slug: t for t in EvidenceTag.objects.filter(slug__in=by_tag.keys())}

    assigned: dict[str, Document] = {}
    for slug, docs in by_tag.items():
        tag = tags_by_slug.get(slug)
        if not tag or not docs:
            continue
        latest_doc, *older_docs = docs

        ProjectDocument.objects.get_or_create(
            project=project,
            document=latest_doc,
            defaults={"added_by": project.owner},
        )
        assigned[slug] = latest_doc

        for stale_link in ProjectDocument.objects.filter(
            project=project, document__in=older_docs
        ).select_related("document"):
            current = effective_tags(stale_link)
            if any(t.slug == slug for t in current):
                override_tags(stale_link, [t for t in current if t.slug != slug])

    return assigned
