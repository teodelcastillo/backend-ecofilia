from __future__ import annotations

from typing import Iterable

from django.db.models import Q, QuerySet

from apps.document.models import Document, EvidenceTag


def accessible_documents_for(user, slugs: Iterable[str]) -> QuerySet[Document]:
    """
    Returns the subset of documents identified by `slugs` that the user can access.
    Includes:
    - Own documents
    - Public documents
    - Documents shared directly with user
    - Documents in projects shared with user
    """
    qs = Document.objects.filter(slug__in=slugs)
    if user.is_staff:
        return qs
    
    # Documents in projects shared with user
    from apps.project.models import ProjectShare
    shared_project_ids = ProjectShare.objects.filter(
        user=user
    ).values_list('project_id', flat=True)
    
    return qs.filter(
        Q(owner=user) 
        | Q(is_public=True) 
        | Q(shares__user=user)
        | Q(projects__id__in=shared_project_ids)
    ).distinct()


def accessible_library_documents(user) -> QuerySet[Document]:
    """
    Full library visibility for the authenticated user (same rules as
    ``DocumentListAPIView`` with scope ``all`` for non-staff).
    """
    qs = Document.objects.all()
    if user.is_staff:
        return qs
    from apps.project.models import ProjectShare

    shared_project_ids = ProjectShare.objects.filter(user=user).values_list(
        "project_id", flat=True
    )
    return qs.filter(
        Q(owner=user)
        | Q(is_public=True)
        | Q(shares__user=user)
        | Q(projects__id__in=shared_project_ids)
    ).distinct()



def default_evidence_tags_for(document: Document) -> list[EvidenceTag]:
    """
    Etiquetas que se proponen al vincular ``document`` a una operación.

    Es el puente entre la biblioteca y la operación: lo que en la biblioteca
    CAF es una carpeta (``topics``), acá llega como etiqueta ya marcada. La
    propuesta no es una decisión — el ejecutivo la corrige desde la operación,
    y a partir de ahí manda lo que quedó asentado ahí.

    Un documento sin topics reconocibles no recibe ninguna: quedar sin etiqueta
    es un estado válido, no un error. Esos documentos siguen entrando en los
    pasos que leen todo el expediente y en los que los nombran explícitamente.
    """
    topics = _topic_keys(document.topics)
    if not topics:
        return []
    return [
        tag
        for tag in EvidenceTag.objects.exclude(source_topics=[])
        if topics.intersection(_topic_keys(tag.source_topics))
    ]


def _topic_keys(topics) -> set[str]:
    """Las formas comparables de un topic: el valor entero y su acrónimo.

    Los topics de la biblioteca CAF vienen como ``"ndcs: contribuciones
    determinadas a nivel nacional"``, pero se los nombra por el acrónimo. Se
    guardan las dos formas para que una etiqueta sembrada como ``"ndcs"``
    reconozca al documento sin depender de cómo se tipeó la carpeta.
    """
    keys: set[str] = set()
    for raw in topics or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        value = raw.lower().strip()
        keys.add(value)
        head, sep, _ = value.partition(":")
        if sep and head.strip():
            keys.add(head.strip())
    return keys
