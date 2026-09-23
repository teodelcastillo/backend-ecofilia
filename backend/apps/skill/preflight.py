"""
Qué revisar antes de lanzar un workflow sobre una operación.

Un agente que corre con un documento a medio indexar lee un corpus incompleto
sin decirlo: eso bloquea. Lo demás advierte y queda registrado en la corrida,
porque puede ser deliberado pero cambia el resultado:

- sin objetivo ni componentes, cada paso pierde el contexto de qué financia la
  operación (entran en el system prompt, ver ``operation_context``);
- un paso que pide una etiqueta que ningún documento tiene corre sólo con el
  documento principal y declara la evidencia faltante.

El frontend mostraba lo primero y parte de lo tercero, pero la API aceptaba
cualquier corrida: un cliente que no lo chequeara lanzaba igual.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from apps.document.models import ChunkingStatus

_PROCESSING = (ChunkingStatus.PENDING, ChunkingStatus.PROCESSING)


@dataclass
class Preflight:
    blocking: list[str] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)


def check_operation_run(skill, project, document_slugs: list[str] | None = None) -> Preflight:
    from apps.project.models import ProjectDocument
    from apps.project.services.evidence_tags import effective_tag_slugs
    from apps.skill.models import SkillType, StepEvidenceSelection

    result = Preflight()
    links = ProjectDocument.objects.filter(project=project).select_related("document").prefetch_related(
        "tags", "document__evidence_tags"
    )
    if document_slugs:
        links = links.filter(document__slug__in=document_slugs)
    links = list(links)

    processing = [link.document.name for link in links if link.document.chunking_status in _PROCESSING]
    if processing:
        result.blocking.append(
            "Todavía se están procesando: " + ", ".join(processing)
            + ". Esperá a que terminen para ejecutar el agente."
        )

    if skill.skill_type != SkillType.COPILOT:
        return result

    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    missing_notes = [k for k in ("objetivo", "componentes") if not str(notes.get(k) or "").strip()]
    if missing_notes:
        result.warnings.append(
            {
                "code": "sin_objetivo_componentes",
                "fields": missing_notes,
                "message": "La operación no tiene "
                + " ni ".join(missing_notes)
                + ": los pasos del análisis no sabrán qué financia.",
            }
        )

    available = {slug for link in links for slug in effective_tag_slugs(link)}
    requested = {
        tag
        for step in skill.steps.all()
        if step.evidence_selection == StepEvidenceSelection.TAGGED
        for tag in (step.evidence_tags or [])
    }
    missing_tags = sorted(requested - available)
    if missing_tags:
        result.warnings.append(
            {
                "code": "etiquetas_faltantes",
                "tags": missing_tags,
                "message": "Ningún documento de la operación es de tipo: "
                + ", ".join(missing_tags)
                + ". Los pasos que los piden correrán sin esa evidencia.",
            }
        )
    return result
