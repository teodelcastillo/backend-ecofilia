from __future__ import annotations

import re
import unicodedata
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
    Etiquetas que sugieren los temas (``topics``) de ``document``.

    Ya no es la vía principal: la etiqueta la elige quien carga el documento.
    Esto queda para lo que se cargó sin etiqueta —la biblioteca vieja, o un
    documento al que sólo le pusieron temas—, como propuesta que se guarda en
    el documento y que cualquiera puede corregir.

    La comparación ignora mayúsculas y acentos de los dos lados. Antes los
    comparaba tal cual, y como los ``source_topics`` sembrados están sin tilde
    ("metodologia caf", "guias sectoriales"), un tema escrito como se escribe
    —"metodología caf"— no proponía nunca nada.

    Sigue siendo **asimétrica**. Del lado del documento el tema es texto libre,
    así que se fragmenta en palabras para tolerar "ndc colombia 2023". Del lado
    de la etiqueta el valor es curado: una palabra suelta tiene que coincidir
    con un fragmento, y una frase ("comunicacion nacional") tiene que aparecer
    entera dentro del tema. Fragmentarla también haría que cualquier tema con
    "nacional" propusiera la etiqueta.
    """
    topics = _document_topic_keys(document.topics)
    if not topics:
        return []
    return [
        tag
        for tag in EvidenceTag.objects.exclude(source_topics=[])
        if _tag_matches(tag.source_topics, topics)
    ]


def normalize_topic(value: str) -> str:
    """Minúsculas, sin acentos y con espacios colapsados."""
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.lower().split())


def _tag_matches(source_topics, doc_keys: set[str]) -> bool:
    for raw in source_topics or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        wanted = normalize_topic(raw)
        if wanted in doc_keys:
            return True
        if " " in wanted and any(
            re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", key) for key in doc_keys
        ):
            return True
    return False


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
        value = normalize_topic(raw)
        keys.add(value)
        head, sep, _ = value.partition(":")
        if sep and head.strip():
            keys.add(head.strip())
        for frag in _TOPIC_SPLIT_RE.split(value):
            if len(frag) >= _MIN_TOKEN_LEN:
                keys.add(frag)
    return keys
