"""
Etiquetas de evidencia de los documentos de una operación.

La etiqueta vive en el documento (``Document.evidence_tags``): la pone quien lo
carga, y toda operación que lo vincule la hereda. No hay un paso de "proponer"
al vincular ni nada que sincronizar después — el vínculo lee las del documento
en el momento en que se las pide, así que un documento que se etiqueta hoy ya
cuenta en todas las operaciones que lo tienen, incluidas las de ayer.

La operación puede apartarse (``ProjectDocument.tags_overridden``) cuando un
documento cumple ahí otro papel. Desde ese momento manda lo que se decidió en
la operación, hasta que alguien vuelva a las del documento.
"""
from __future__ import annotations

from typing import Iterable

from django.db.models import Q, QuerySet

from apps.document.models import EvidenceTag
from apps.document.services import evidence_tags_for_organization, resolve_evidence_tags
from apps.project.models import ProjectDocument


def effective_tags(link: ProjectDocument) -> list[EvidenceTag]:
    """Las etiquetas con las que cuenta este vínculo en la operación.

    Lee de ``.all()`` para aprovechar un ``prefetch_related`` del llamador:
    el serializador de la operación lo llama una vez por documento.
    """
    source = link.tags if link.tags_overridden else link.document.evidence_tags
    return sorted(source.all(), key=lambda t: (t.position, t.name))


def effective_tag_slugs(link: ProjectDocument) -> list[str]:
    return [t.slug for t in effective_tags(link)]


def project_evidence_tags(project_id: int) -> QuerySet[EvidenceTag]:
    """Las etiquetas que valen en la operación: globales más las de su institución.

    Una etiqueta de otra institución no cuenta acá aunque un documento
    vinculado la tenga: los slugs se resuelven siempre en este conjunto.
    """
    from apps.project.models import Project

    organization_id = (
        Project.objects.filter(pk=project_id).values_list("organization_id", flat=True).first()
    )
    return evidence_tags_for_organization(organization_id)


def tagged_in_project_q(project_id: int, slugs: Iterable[str]) -> Q:
    """Filtro de ``Document`` para los que en la operación cuentan con ``slugs``.

    Cada lado va como subconsulta propia y no como un único ``filter`` sobre
    las dos relaciones: son dos muchos-a-muchos distintos, y cruzarlos en un
    mismo JOIN matchearía un documento heredado por la etiqueta de otro
    vínculo sobreescrito.

    Los slugs se resuelven dentro de las etiquetas de la operación (globales
    más las de su institución, ver ``project_evidence_tags``).
    """
    tags = project_evidence_tags(project_id).filter(slug__in=list(slugs)).values("pk")
    inherited = ProjectDocument.objects.filter(
        project_id=project_id,
        tags_overridden=False,
        document__evidence_tags__in=tags,
    ).values("document_id")
    overridden = ProjectDocument.objects.filter(
        project_id=project_id,
        tags_overridden=True,
        tags__in=tags,
    ).values("document_id")
    return Q(id__in=inherited) | Q(id__in=overridden)


def override_tags(link: ProjectDocument, tags: Iterable[EvidenceTag]) -> None:
    """La operación decide sus propias etiquetas para este documento."""
    link.tags.set(list(tags))
    if not link.tags_overridden:
        link.tags_overridden = True
        link.save(update_fields=["tags_overridden"])


def inherit_tags(link: ProjectDocument) -> None:
    """Vuelve a usar las etiquetas del documento."""
    link.tags.clear()
    if link.tags_overridden:
        link.tags_overridden = False
        link.save(update_fields=["tags_overridden"])


def ensure_principal_stage_tag(project, link: ProjectDocument | None = None) -> str:
    """El documento principal lleva la etiqueta de la etapa en que se cargó.

    Al marcarlo como principal, si no tiene ``ido`` ni ``dec`` se le pone la de
    la etapa actual de la operación según su institución
    (``stages[].principal_tag``; en CAF, IDO → ``ido``, DEC → ``dec``), como
    decisión de la operación y no del documento: el mismo archivo puede ser el
    IDO de una operación y un anexo en otra. En pre-originación no hay etiqueta
    de etapa, y un documento que ya trae una (p. ej. marcado IDO en IDO y
    todavía principal en DEC) no se cambia.

    Devuelve el slug que puso, o vacío si no hizo falta.
    """
    from apps.project.services.stages import current_stage
    from apps.user import institution_config

    if link is None:
        if not project.blueprint_document_id:
            return ""
        link = (
            ProjectDocument.objects.filter(project=project, document_id=project.blueprint_document_id)
            .select_related("document")
            .first()
        )
        if link is None:
            return ""
    config = institution_config.config_for_project(project)
    slug = institution_config.principal_tag(config, current_stage(project)) or ""
    if not slug:
        return ""
    current = effective_tags(link)
    if any(tag.slug in institution_config.principal_tags(config) for tag in current):
        return ""
    tag = resolve_evidence_tags(
        [slug], evidence_tags_for_organization(project.organization_id), project.organization_id
    ).get(slug)
    if tag is None:
        return ""
    override_tags(link, [*current, tag])
    return slug
