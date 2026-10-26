"""
Los grupos con los que el portal muestra las etiquetas de evidencia.

Salen del slug y no de una columna: el catálogo es ampliable desde la
aplicación y una etiqueta nueva cae en «otro» sin que nadie la clasifique.
La interfaz filtra por grupo (el principal se marca IDO o DEC, los documentos
de la operación eligen entre las de operación) en vez de llevar listas duras.
"""
from __future__ import annotations

GROUP_PRINCIPAL = "principal"
GROUP_OPERACION = "operacion"
GROUP_CONTEXTO = "contexto"
GROUP_OTRO = "otro"

# Las etiquetas del documento principal: la etapa de la operación en la que se
# cargó. Las claves son las etapas (``context_notes["estado"]``).
PRINCIPAL_TAG_BY_STAGE: dict[str, str] = {"ido_cci": "ido", "dec": "dec"}

_GROUPS: dict[str, tuple[str, ...]] = {
    GROUP_PRINCIPAL: ("ido", "dec"),
    GROUP_OPERACION: (
        "ias",
        "analisis-riesgos",
        "matriz-resultados",
        "disenos-proyecto",
        "ibat",
        "documento-operacion",
    ),
    GROUP_CONTEXTO: (
        "ndc",
        "nap",
        "lts",
        "comunicacion-adaptacion",
        "nbsap",
        "pancd",
        "inventario-gei",
        "ley-cambio-climatico",
        "reporte-nacional",
    ),
}

_GROUP_BY_SLUG: dict[str, str] = {
    slug: group for group, slugs in _GROUPS.items() for slug in slugs
}


def evidence_tag_group(slug: str) -> str:
    return _GROUP_BY_SLUG.get(slug or "", GROUP_OTRO)


def principal_tag_slugs() -> tuple[str, ...]:
    return _GROUPS[GROUP_PRINCIPAL]
