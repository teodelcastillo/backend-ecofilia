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

from django.db.models import Q

from apps.document.models import EvidenceTag
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


def tagged_in_project_q(project_id: int, slugs: Iterable[str]) -> Q:
    """Filtro de ``Document`` para los que en la operación cuentan con ``slugs``.

    Cada lado va como subconsulta propia y no como un único ``filter`` sobre
    las dos relaciones: son dos muchos-a-muchos distintos, y cruzarlos en un
    mismo JOIN matchearía un documento heredado por la etiqueta de otro
    vínculo sobreescrito.
    """
    slugs = list(slugs)
    inherited = ProjectDocument.objects.filter(
        project_id=project_id,
        tags_overridden=False,
        document__evidence_tags__slug__in=slugs,
    ).values("document_id")
    overridden = ProjectDocument.objects.filter(
        project_id=project_id,
        tags_overridden=True,
        tags__slug__in=slugs,
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
