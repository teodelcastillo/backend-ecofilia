"""Las NDC vigentes, leídas de la biblioteca pública.

Es el único punto en que el módulo mira fuera de sus tablas, y sólo lee: las
NDC ya están cargadas, nombradas y etiquetadas en la biblioteca pública, y
mantener una copia aparte era tener dos lugares que actualizar cuando un país
presenta una NDC nueva.

Una NDC es un documento público con la etiqueta global ``ndc`` y el país en
``region``. Si un país tiene varias, vale la más nueva: por año del documento
y, a igualdad o sin año, por fecha de carga (la misma regla que usa el resto
de la plataforma para los instrumentos de país).
"""
from __future__ import annotations

from django.db.models import F

from apps.document.models import Document
from apps.proyeccion.lectura import plegar

ETIQUETA_NDC = "ndc"


def ndc_vigentes() -> dict[str, Document]:
    """La NDC vigente de cada país (clave: el país sin acentos ni mayúsculas).

    No trae el texto: se pide aparte, sólo para las que se van a leer
    (``texto_de``).
    """
    documentos = (
        Document.objects.filter(
            is_public=True,
            evidence_tags__slug=ETIQUETA_NDC,
            evidence_tags__organization__isnull=True,
            region__isnull=False,
        )
        .exclude(extracted_text="")
        .defer("extracted_text")
        .order_by(F("year").desc(nulls_last=True), "-created_at")
        .distinct()
    )
    vigentes: dict[str, Document] = {}
    for documento in documentos:
        clave = plegar(documento.region or "")
        if clave and clave not in vigentes:
            vigentes[clave] = documento
    return vigentes


def ndc_de(pais: str) -> Document | None:
    return ndc_vigentes().get(plegar(pais))


def texto_de(documento: Document) -> str:
    return (
        Document.objects.filter(pk=documento.pk).values_list("extracted_text", flat=True).first() or ""
    ).replace("\x00", "")
