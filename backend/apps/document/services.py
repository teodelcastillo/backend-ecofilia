from __future__ import annotations

import re
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

    La comparación es deliberadamente **asimétrica**. Del lado del documento
    (``_document_topic_keys``) el topic es texto libre —``edit-topics-dialog``
    no valida contra ningún vocabulario, cada bibliotecario tipea lo que
    quiere— así que se fragmenta en palabras para tolerar variantes como
    ``"ndc colombia 2023"`` o ``"ndc-actualizada"``. Del lado de la etiqueta
    (``tag.source_topics``) el valor es curado a propósito y se usa tal cual:
    si se fragmentara igual, una etiqueta con ``"comunicación nacional"``
    matchearía cualquier documento que mencione "nacional" en su topic, que es
    exactamente el falso positivo que la etiqueta viene a evitar.
    """
    topics = _document_topic_keys(document.topics)
    if not topics:
        return []
    return [
        tag
        for tag in EvidenceTag.objects.exclude(source_topics=[])
        if topics.intersection(_tag_topic_keys(tag.source_topics))
    ]


def _tag_topic_keys(topics) -> set[str]:
    """Las formas comparables de un ``source_topics`` de etiqueta: exacto."""
    return {t.lower().strip() for t in (topics or []) if isinstance(t, str) and t.strip()}


# Separadores entre los que se puede fragmentar un topic de biblioteca:
# espacio, coma, punto y coma, guion, guion bajo, barra y paréntesis. Dos
# puntos se maneja aparte porque además define el prefijo/acrónimo.
_TOPIC_SPLIT_RE = re.compile(r"[,;/()_\-\s]+")

# Piso de largo para que un fragmento cuente como token. Sin esto, una frase
# que casualmente contuviera "ac" suelto (una etiqueta real, de sólo dos
# letras) dispararía un falso positivo; tres caracteres alcanza para las
# siglas reales del dominio (ndc, nap, lts, bur, ias...) sin abrir la puerta a
# fragmentos cortos accidentales.
_MIN_TOKEN_LEN = 3


def _document_topic_keys(topics) -> set[str]:
    """
    Las formas comparables de un topic de documento: el valor entero, su
    acrónimo antes de ``:``, y sus fragmentos de 3+ caracteres.

    Los topics de la biblioteca CAF vienen como ``"ndcs: contribuciones
    determinadas a nivel nacional"``, pero se los nombra por el acrónimo — de
    ahí el prefijo. El resto de los fragmentos es la tolerancia a que no todo
    topic siga ese formato: no hay un vocabulario controlado que lo garantice.
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
        for frag in _TOPIC_SPLIT_RE.split(value):
            if len(frag) >= _MIN_TOKEN_LEN:
                keys.add(frag)
    return keys
