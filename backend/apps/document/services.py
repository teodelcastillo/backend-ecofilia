from __future__ import annotations

import re
import unicodedata
from typing import Iterable

from django.db.models import Q, QuerySet

from apps.document.models import Document, EvidenceTag


def visible_documents_q(user, prefix: str = "") -> Q:
    """
    Qué documentos ve un usuario que no es staff, como condición de filtro.

    Los propios, los públicos, los compartidos con él y los de cualquier
    operación que ve (como dueño, colaborador o administrador de la
    institución); el administrador de una institución, además, los de sus
    miembros. Antes
    sólo contaban las operaciones compartidas con él: el dueño de una
    operación no veía los documentos que un colaborador había subido a su
    propia operación (404 al abrirlos, 400 al chatear con ellos, y el RAG los
    descartaba en silencio), mientras que un lector de esa misma operación sí
    los veía.

    ``prefix`` es el camino hasta el documento desde el modelo que se filtra:
    ``"document__"`` para fragmentos.

    Es la única definición de esta regla; el staff se resuelve antes, en cada
    llamador, porque ve todo.
    """
    from apps.project.models import Project
    from apps.user.access import is_organization_admin

    # Las operaciones que el usuario ve: las suyas, las compartidas con él y,
    # si administra una institución, todas las de ella.
    project_ids = Project.objects.for_user(user).values("pk")
    visible = (
        Q(**{f"{prefix}owner": user})
        | Q(**{f"{prefix}is_public": True})
        | Q(**{f"{prefix}shares__user": user})
        | Q(**{f"{prefix}projects__id__in": project_ids})
    )
    # El administrador de una institución ve también los documentos de sus
    # miembros.
    if is_organization_admin(user):
        visible |= Q(**{f"{prefix}owner__organization_id": user.organization_id})
    return visible


def accessible_documents_for(user, slugs: Iterable[str]) -> QuerySet[Document]:
    """
    Returns the subset of documents identified by `slugs` that the user can
    access (see ``visible_documents_q``).
    """
    qs = Document.objects.filter(slug__in=slugs)
    if user.is_staff:
        return qs
    return qs.filter(visible_documents_q(user)).distinct()


def accessible_library_documents(user) -> QuerySet[Document]:
    """
    Full library visibility for the authenticated user (same rules as
    ``DocumentListAPIView`` with scope ``all`` for non-staff).
    """
    qs = Document.objects.all()
    if user.is_staff:
        return qs
    return qs.filter(visible_documents_q(user)).distinct()



# ---------------------------------------------------------------------------
# Etiquetas de evidencia por institución
#
# Una etiqueta sin institución es global (los instrumentos de país: NDC, NAP…);
# con institución, es de esa institución y ninguna otra la ve. El slug es único
# dentro de «las globales más las de una institución», así que resolver un slug
# siempre es resolverlo en ese conjunto.
# ---------------------------------------------------------------------------

def evidence_tags_for_organization(organization_id) -> QuerySet[EvidenceTag]:
    """Las etiquetas que ve una institución: las globales más las suyas."""
    visible = Q(organization__isnull=True)
    if organization_id:
        visible |= Q(organization_id=organization_id)
    return EvidenceTag.objects.filter(visible)


def visible_evidence_tags(user) -> QuerySet[EvidenceTag]:
    """Las del catálogo que ve ``user``: el superadmin, todas."""
    from apps.user.access import is_ecofilia_admin

    if user is None:
        return EvidenceTag.objects.all()
    if getattr(user, "is_staff", False) or is_ecofilia_admin(user):
        return EvidenceTag.objects.all()
    return evidence_tags_for_organization(getattr(user, "organization_id", None))


def resolve_evidence_tags(
    slugs: Iterable[str],
    queryset: QuerySet[EvidenceTag],
    prefer_organization_id=None,
) -> dict[str, EvidenceTag]:
    """``{slug: etiqueta}`` de los ``slugs`` que existen en ``queryset``.

    Si un slug aparece más de una vez (sólo pasa en la vista del superadmin,
    que ve todas), gana la de ``prefer_organization_id``, después la global y
    después la más vieja: nunca una elección al azar.
    """
    slugs = list(dict.fromkeys(slugs))
    found: dict[str, EvidenceTag] = {}

    def rank(tag: EvidenceTag) -> tuple:
        return (
            0 if prefer_organization_id and tag.organization_id == prefer_organization_id else 1,
            0 if tag.organization_id is None else 1,
            tag.pk,
        )

    for tag in queryset.filter(slug__in=slugs):
        current = found.get(tag.slug)
        if current is None or rank(tag) < rank(current):
            found[tag.slug] = tag
    return found


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
    # Sólo las que ve la institución del dueño: un documento no puede quedar
    # con la etiqueta de otra institución.
    from django.contrib.auth import get_user_model

    owner_org = (
        get_user_model().objects.filter(pk=document.owner_id)
        .values_list("organization_id", flat=True).first()
    )
    return [
        tag
        for tag in evidence_tags_for_organization(owner_org).exclude(source_topics=[])
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
