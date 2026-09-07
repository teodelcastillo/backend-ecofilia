"""
Etiquetas de evidencia de los documentos de una operación.

La etiqueta se propone sola al vincular —la biblioteca ya sabe que un
documento es una NDC— y a partir de ahí la operación manda: quien la edita
desde el portal está diciendo qué papel cumple ese documento *acá*, que no
tiene por qué ser el que sugiere su ficha en la biblioteca.
"""
from __future__ import annotations

from apps.document.services import default_evidence_tags_for
from apps.project.models import ProjectDocument


def apply_default_tags(link: ProjectDocument) -> list:
    """
    Pre-marca las etiquetas que sugieren los topics del documento.

    Sólo actúa sobre vínculos sin etiquetas: si alguien ya decidió algo para
    este documento en esta operación, una re-vinculación no se lo pisa.
    """
    if link.tags.exists():
        return []
    tags = default_evidence_tags_for(link.document)
    if tags:
        link.tags.set(tags)
    return tags
