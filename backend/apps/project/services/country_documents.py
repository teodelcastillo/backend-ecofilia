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

from apps.document.models import Document, EvidenceTag
from apps.document.services import accessible_library_documents, default_evidence_tags_for
from apps.project.models import Project, ProjectDocument

# Ampliable: cualquier etiqueta de la biblioteca donde "un documento por
# país, el más nuevo gana" tenga sentido puede sumarse acá (ver el catálogo
# sembrado en apps/document/migrations/0016_seed_evidence_tags.py — nbsap y
# pancd son candidatos razonables si en algún momento se pide extenderlo).
COUNTRY_INSTRUMENT_TAG_SLUGS = ["ndc", "nap", "lts", "comunicacion-adaptacion"]


def country_instrument_documents(
    user, region: str, tag_slugs=COUNTRY_INSTRUMENT_TAG_SLUGS
) -> dict[str, list[Document]]:
    """
    Documentos de ``region`` accesibles para ``user`` que matchean cada
    etiqueta, del más nuevo al más viejo.

    Reusa ``default_evidence_tags_for`` — la misma heurística que ya propone
    etiquetas al vincular un documento a mano — así el auto-assign nunca
    diverge de lo que un ejecutivo vería sugerido si lo hiciera él mismo.
    Filtrado por accesibilidad (propios + públicos + compartidos, igual que
    el resto de la biblioteca) para no auto-vincular un documento privado de
    otro usuario a una operación ajena.
    """
    result: dict[str, list[Document]] = {slug: [] for slug in tag_slugs}
    if not region:
        return result
    valid_slugs = set(
        EvidenceTag.objects.filter(slug__in=tag_slugs)
        .exclude(source_topics=[])
        .values_list("slug", flat=True)
    )
    if not valid_slugs:
        return result
    candidates = accessible_library_documents(user).filter(region=region).order_by(
        "-created_at"
    )
    for doc in candidates:
        matched_slugs = {tag.slug for tag in default_evidence_tags_for(doc)}
        for slug in matched_slugs & valid_slugs:
            result[slug].append(doc)
    return result


def sync_country_instrument_documents(project: Project) -> dict[str, Document]:
    """
    Vincula a ``project`` el documento vigente de cada instrumento país según
    ``context_notes["pais"]`` y le pone la etiqueta correspondiente.

    Si una versión más vieja del mismo instrumento había quedado vinculada
    de una sincronización anterior, le saca la etiqueta (sin desvincularla:
    puede seguir citada en la operación por otro motivo) para que sólo la
    vigente quede marcada como "la" NDC/NAP/LTS/AC de la operación — si no,
    dos corridas sucesivas con una NDC nueva en la biblioteca dejarían ambas
    versiones etiquetadas y el paso que pide "la NDC" quedaría ambiguo.

    Se llama al crear o guardar una operación con país cargado — no hace
    falta pedirlo a mano, y repetirlo no tiene costo: sin novedades en la
    biblioteca es un no-op.
    """
    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    region = notes.get("pais")
    if not isinstance(region, str) or not region.strip():
        return {}
    region = region.strip()

    by_tag = country_instrument_documents(project.owner, region)
    tags_by_slug = {t.slug: t for t in EvidenceTag.objects.filter(slug__in=by_tag.keys())}

    assigned: dict[str, Document] = {}
    for slug, docs in by_tag.items():
        tag = tags_by_slug.get(slug)
        if not tag or not docs:
            continue
        latest_doc, *older_docs = docs

        link, _ = ProjectDocument.objects.get_or_create(
            project=project,
            document=latest_doc,
            defaults={"added_by": project.owner},
        )
        link.tags.add(tag)
        assigned[slug] = latest_doc

        if older_docs:
            for stale_link in ProjectDocument.objects.filter(
                project=project, document__in=older_docs, tags=tag
            ):
                stale_link.tags.remove(tag)

    return assigned
