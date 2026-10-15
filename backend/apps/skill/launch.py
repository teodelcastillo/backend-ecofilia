"""
Armar y encolar una corrida de agente.

Hasta ahora la única puerta era el endpoint ``run``: una persona con permiso de
ejecutar elegía el agente y lo lanzaba. El ciclo de una operación agrega otra —
entrar a IDO lanza el reporte de Ecofilia y el insumo; subir el informe del
ejecutivo lanza su lectura; el flujo guiado redacta con el criterio del
ejecutivo— y esas corridas las dispara el flujo institucional, no alguien con
permiso de ejecutar asistentes.

Las dos puertas comparten acá el armado de la corrida (modo de salida,
metadata, etapa) para que no haya dos versiones de "cómo se crea una
ejecución". Los permisos no viven acá: los decide cada puerta.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from apps.skill import parameters as parameters_module
from apps.skill.dispatch import dispatch_execution
from apps.skill.models import (
    ExecutionOutputMode,
    ExecutionStatus,
    Skill,
    SkillExecution,
    SkillType,
)
from apps.skill.table_schema import schema_has_columns

logger = logging.getLogger(__name__)


def stage_of(project) -> str:
    """La etapa de la operación (``context_notes.estado``), o vacío."""
    if project is None:
        return ""
    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    value = notes.get("estado")
    return value.strip() if isinstance(value, str) else ""


def resolve_effective_output(
    *,
    skill: Skill,
    run_output_mode=None,
    run_table_schema: dict | None = None,
) -> tuple[str, dict]:
    """
    Resolve the effective output_mode and table_schema for a run.

    Precedence:
      1. Explicit `output_mode` in the run payload (with optional override schema).
      2. Skill `default_output_mode` (and its persisted `table_schema`).
      3. Defaults to TEXT.

    For Copilots the *skill-level* table_schema is intentionally ignored;
    each step decides its own output_mode/table_schema, so we only honor a
    run-level override when the user explicitly requests a single tabular
    output for the whole copilot.
    """
    run_table_schema = run_table_schema or {}
    if run_output_mode:
        mode = run_output_mode
    else:
        mode = skill.default_output_mode or ExecutionOutputMode.TEXT

    if mode == ExecutionOutputMode.TABLE:
        if skill.skill_type == SkillType.COPILOT and not run_table_schema:
            raise ValueError(
                "Copilots produce tabular output per step. "
                "Either configure step output_mode='table' on the skill, or "
                "send a run-level table_schema to consolidate the result."
            )
        schema = run_table_schema or skill.table_schema or {}
        if not schema_has_columns(schema):
            raise ValueError(
                "Cannot run skill in table mode: no table_schema is configured."
            )
        return mode, schema
    return ExecutionOutputMode.TEXT, {}


def create_execution(
    *,
    skill: Skill,
    owner,
    project=None,
    repository=None,
    document=None,
    extra_instructions: str = "",
    input_values: dict | None = None,
    output_mode: str = ExecutionOutputMode.TEXT,
    table_schema: dict | None = None,
    document_slugs: list[str] | None = None,
    step_document_overrides: dict | None = None,
    review_each_step: bool = False,
    preflight_warnings: list | None = None,
    input_value_warnings: list | None = None,
    stage: str | None = None,
    extra_metadata: dict | None = None,
) -> SkillExecution:
    """Crea la corrida en ``pending``. No la encola."""
    table_schema = table_schema or {}
    metadata = {
        "table_columns": [
            col.get("key")
            for col in (table_schema.get("columns") or [])
            if isinstance(col, dict) and col.get("key")
        ],
        "table_schema": table_schema,
        "document_slugs_filter": list(document_slugs or []),
        "step_document_overrides": step_document_overrides or {},
        "review_each_step": bool(review_each_step),
        # Lo que el chequeo previo advirtió y se lanzó igual: explica
        # después por qué un paso salió sin evidencia o sin contexto.
        **({"preflight_warnings": preflight_warnings} if preflight_warnings else {}),
        # Claves que no corresponden a ningún parámetro declarado. No
        # bloquean —romperían a cualquier llamador que hoy manda campos
        # de más— pero quedan registradas: casi siempre son un typo, y
        # un typo acá deja un `{{token}}` sin resolver en el prompt.
        **({"input_value_warnings": input_value_warnings} if input_value_warnings else {}),
        **(extra_metadata or {}),
    }
    return SkillExecution.objects.create(
        skill=skill,
        owner=owner,
        repository=repository,
        project=project,
        document=document,
        extra_instructions=extra_instructions,
        input_values=input_values or {},
        output_mode=output_mode,
        metadata=metadata,
        status=ExecutionStatus.PENDING,
        stage=stage_of(project) if stage is None else stage,
    )


@dataclass
class LaunchOutcome:
    """Qué pasó con un lanzamiento institucional."""

    status: str  # launched | blocked | failed
    execution: SkillExecution | None = None
    reason: str = ""

    def as_dict(self, skill_slug: str) -> dict:
        return {
            "skill_slug": skill_slug,
            "status": self.status,
            "execution_id": self.execution.id if self.execution else None,
            "reason": self.reason,
        }


def launch_institutional(
    skill: Skill,
    *,
    owner,
    project=None,
    document=None,
    stage: str | None = None,
    input_values: dict | None = None,
    launched_by: str,
) -> LaunchOutcome:
    """
    Lanza ``skill`` como parte del flujo de la operación.

    No pide permiso de ejecutar asistentes: lo decide quien llama (por ejemplo,
    cambiar de etapa es de quien edita la operación). Sí aplica lo que protege
    el resultado: parámetros válidos y el chequeo previo de la operación —un
    agente que lee documentos a medio procesar escribe un informe incompleto
    sin avisar—. Si algo de eso falla, la corrida no se crea y vuelve el motivo.

    ``owner`` queda como dueño de la corrida. Eso no le da permiso de
    relanzarla: ``rerun`` y ``resume`` exigen además poder ejecutar asistentes.
    """
    resolved, issues = parameters_module.resolve_input_values(
        skill.parameters.all(), input_values or {}
    )
    blocking = parameters_module.blocking_issues(issues)
    if blocking:
        return LaunchOutcome(
            status="blocked",
            reason=" ".join(parameters_module.describe_issue(i) for i in blocking),
        )

    try:
        output_mode, table_schema = resolve_effective_output(skill=skill)
    except ValueError as exc:
        return LaunchOutcome(status="blocked", reason=str(exc))

    preflight_warnings = None
    if project is not None:
        from apps.skill.preflight import check_operation_run

        preflight = check_operation_run(skill, project)
        if preflight.blocking:
            return LaunchOutcome(status="blocked", reason=" ".join(preflight.blocking))
        preflight_warnings = preflight.warnings or None
        # El run de la API exige que el agente esté habilitado en la
        # operación; el flujo institucional lo habilita para que después un
        # superadmin pueda repetirlo desde la misma operación.
        project.enabled_skills.add(skill)

    execution = create_execution(
        skill=skill,
        owner=owner,
        project=project,
        document=document,
        input_values=resolved,
        output_mode=output_mode,
        table_schema=table_schema,
        preflight_warnings=preflight_warnings,
        input_value_warnings=issues or None,
        stage=stage,
        extra_metadata={"launched_by": launched_by},
    )
    outcome = LaunchOutcome(status="launched", execution=execution)

    def _dispatch() -> None:
        if not dispatch_execution(execution.id):
            execution.refresh_from_db()
            outcome.status = "failed"
            outcome.reason = execution.error_message or "No se pudo encolar la corrida."

    # Encolar dentro de una transacción abierta dejaría al worker buscando una
    # fila que todavía no se ve. Fuera de una transacción, `on_commit` corre en
    # el acto y el resultado del encolado ya está en `outcome` al volver.
    transaction.on_commit(_dispatch)
    return outcome
