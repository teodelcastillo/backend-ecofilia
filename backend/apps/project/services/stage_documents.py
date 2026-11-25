"""
Qué documentos de la operación lee el informe de cada etapa.

Cada etapa con informe es una foto propia de la operación (decisión del
25/11/2026). En CAF, el DEC no lee el documento IDO ni el IET IDO: analiza el
documento DEC y lo que se cargó como evidencia de la operación. Sólo el
contexto de clima y biodiversidad se hereda del IDO, ya escrito
(``stage_inheritance``).

Como el asistente del DEC es una copia del de IDO, sus pasos piden la etiqueta
del documento principal de IDO (``ido``). En una corrida de DEC, pedir el
principal de otra etapa es pedir el de la etapa: ``ido`` se lee como ``dec``.
Así la copia funciona tal cual, sin retocar cada paso en el Studio.
"""
from __future__ import annotations

from apps.project.models import Project
from apps.user import institution_config


def _etiquetas(project: Project, stage: str | None) -> tuple[str | None, set[str]]:
    """La etiqueta del principal de ``stage`` y las de las demás etapas."""
    if not stage:
        return None, set()
    config = institution_config.config_for_project(project)
    if stage not in institution_config.report_stages(config):
        return None, set()
    propia = institution_config.principal_tag(config, stage)
    if not propia:
        return None, set()
    return propia, institution_config.principal_tags(config) - {propia}


def etiqueta_principal(project: Project, stage: str | None) -> str | None:
    """La etiqueta del documento principal de ``stage`` (en DEC, ``dec``)."""
    return _etiquetas(project, stage)[0]


def etiquetas_equivalentes(project: Project, stage: str | None) -> dict[str, str]:
    """``{etiqueta del principal de otra etapa: la de esta}``; vacío si no aplica."""
    propia, otras = _etiquetas(project, stage)
    return {otra: propia for otra in otras} if propia else {}


def documentos_de_otras_etapas(project: Project, stage: str | None) -> set[int]:
    """Los documentos de la operación que el informe de ``stage`` no lee.

    Son el documento principal de otra etapa (el IDO, en DEC) y los informes de
    etapas anteriores (el IET IDO), salvo que también lleven la etiqueta de la
    etapa. El documento principal vigente de la operación nunca se excluye.
    """
    from apps.project.models import ProjectDocument
    from apps.project.services.evidence_tags import tagged_in_project_q
    from apps.project.services.stage_inheritance import informes_de_etapas_anteriores
    from apps.document.models import Document

    propia, otras = _etiquetas(project, stage)
    if not propia:
        return set()
    vinculados = Document.objects.filter(
        id__in=ProjectDocument.objects.filter(project=project).values("document_id")
    )
    de_otras = set(
        vinculados.filter(tagged_in_project_q(project.pk, sorted(otras)))
        .exclude(tagged_in_project_q(project.pk, [propia]))
        .values_list("id", flat=True)
    ) if otras else set()
    excluidos = de_otras | informes_de_etapas_anteriores(project)
    excluidos.discard(project.blueprint_document_id)
    return excluidos
