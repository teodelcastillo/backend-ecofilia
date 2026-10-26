"""
Las etapas de una operación: pre-originación → IDO → DEC → aprobada.

La etapa vive en ``context_notes["estado"]``, pero no se edita como un campo
más: cambiarla tiene efectos. Entrar a IDO lanza el reporte de Ecofilia (la
IET), una vez por etapa; el insumo que ve el ejecutivo es la ficha que el
portal arma a partir de esa misma corrida. Por eso el único camino
para cambiarla es :func:`transition` —la edición de la operación preserva la
etapa que tiene el servidor— y cada cambio queda registrado
(``ProjectStageTransition``).

Reglas:

- Avanzar es de quien edita la operación. Volver atrás, del staff de Ecofilia
  o de quien administra la institución: retroceder deshace un hito del ciclo.
- Lo que la etapa lanza se decide en un solo lugar, :func:`stage_workflow_slugs`.
  Ahí entran después las variantes por familia de producto (PPI, PBL…) y la
  configuración desde el Studio.
- Si al entrar a la etapa la operación no está lista (sin documento principal,
  documentos procesándose), lo que no se pudo lanzar queda ``blocked`` y se
  reintenta solo cuando cambia lo que lo bloqueaba (ver
  :func:`retry_pending_stage_launches`).
"""
from __future__ import annotations

import logging

from django.db import transaction

from apps.project.models import Project, ProjectStageTransition
from apps.project.services.alignment_history import record_if_changed

logger = logging.getLogger(__name__)

STAGE_ORDER: list[str] = ["pre_originacion", "ido_cci", "dec", "aprobada"]
STAGE_LABELS: dict[str, str] = {
    "pre_originacion": "Pre-originación",
    "ido_cci": "IDO-CCI",
    "dec": "DEC",
    "aprobada": "Aprobada",
}

# Workflows del ciclo CAF. Los slugs son el contrato con la interfaz, que lee
# sus pasos por título; los workflows se ajustan desde el Studio.
IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
# El insumo fue un agente aparte (``0033``); desde ``0041`` es la ficha de la
# IET. El slug queda para reconocer sus corridas viejas en el historial.
INSUMO_SLUG = "caf-insumo-ejecutivo"
GUIADO_SLUG = "caf-iet-guiado-ejecutivo"
LECTURA_SLUG = "caf-lectura-informe-ejecutivo"


class StageError(ValueError):
    """La transición pedida no tiene sentido (etapa desconocida, la misma…)."""


class StagePermissionError(PermissionError):
    """El usuario no puede hacer esta transición."""


def current_stage(project: Project) -> str:
    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    value = notes.get("estado")
    return value.strip() if isinstance(value, str) else ""


def stage_index(stage: str) -> int:
    """Posición en el ciclo. Una etapa vacía o desconocida va antes de todas."""
    return STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1


def is_ecofilia_staff(user) -> bool:
    """El equipo de Ecofilia: staff de Django o superadmin de la plataforma."""
    from apps.user.access import is_ecofilia_admin

    return bool(getattr(user, "is_staff", False) or is_ecofilia_admin(user))


def can_revert(user, project: Project) -> bool:
    return bool(is_ecofilia_staff(user) or project.is_administered_by(user))


def can_transition(user, project: Project, to_stage: str) -> bool:
    if stage_index(to_stage) > stage_index(current_stage(project)):
        return project.can_edit(user)
    return can_revert(user, project)


def stage_workflow_slugs(project: Project, stage: str) -> list[str]:
    """Qué workflows lanza la operación al entrar a ``stage``.

    Hoy sólo IDO lanza algo. Es el único punto de ruteo a propósito: cuando
    haya variantes por familia de producto, sector o institución, se eligen acá
    a partir de los datos de la operación.
    """
    if stage == "ido_cci":
        return [IET_SLUG]
    return []


def runs_for_stage(project: Project, skill, stage: str):
    """Corridas de ``skill`` sobre la operación que cuentan para ``stage``.

    Las corridas anteriores a que existiera la etapa (``stage=""``) cuentan
    como de IDO: la IET era, por definición, el informe de IDO.
    """
    from apps.skill.models import SkillExecution

    stages = [stage, ""] if stage == "ido_cci" else [stage]
    return SkillExecution.objects.filter(project=project, skill=skill, stage__in=stages)


def launch_stage_workflows(
    project: Project,
    stage: str,
    user,
    *,
    slugs: list[str] | None = None,
    step_instructions: dict[str, dict] | None = None,
) -> list[dict]:
    """Lanza los workflows de ``stage`` que todavía no corrieron.

    ``step_instructions`` son las instrucciones adicionales por paso de un
    superadmin, por workflow: ``{slug: {step_id: texto}}``. Si el lanzamiento
    queda bloqueado (por ejemplo, documentos todavía en proceso), viajan con el
    registro del lanzamiento y se usan cuando se reintenta.

    Una vez por etapa: si ya hay una corrida que no falló, no se repite. Si la
    que hubo falló, tampoco: retomarla (o repetirla) es de un superadmin, que
    puede ver por qué falló antes de gastar otra corrida.
    """
    from apps.skill.launch import launch_institutional
    from apps.skill.models import ExecutionStatus, Skill

    results: list[dict] = []
    for slug in slugs if slugs is not None else stage_workflow_slugs(project, stage):
        skill = Skill.objects.filter(slug=slug).first()
        if skill is None:
            results.append(
                {"skill_slug": slug, "status": "unavailable", "execution_id": None,
                 "reason": "El agente no está disponible en esta instalación."}
            )
            continue

        runs = runs_for_stage(project, skill, stage)
        alive = runs.exclude(status=ExecutionStatus.FAILED).order_by("-created_at").first()
        if alive is not None:
            results.append(
                {"skill_slug": slug, "status": "exists", "execution_id": alive.id, "reason": ""}
            )
            continue
        failed = runs.order_by("-created_at").first()
        if failed is not None:
            results.append(
                {"skill_slug": slug, "status": "failed_before", "execution_id": failed.id,
                 "reason": "La corrida anterior falló: la retoma un superadmin desde Corridas."}
            )
            continue

        if not project.blueprint_document_id:
            results.append(
                {"skill_slug": slug, "status": "blocked", "execution_id": None,
                 "reason": "Falta marcar el documento principal de la operación."}
            )
            continue

        instrucciones = (step_instructions or {}).get(slug) or None
        outcome = launch_institutional(
            skill,
            owner=user,
            project=project,
            stage=stage,
            launched_by="stage",
            step_instructions=instrucciones,
        )
        entry = outcome.as_dict(slug)
        if instrucciones and outcome.status == "blocked":
            entry["step_instructions"] = instrucciones
        results.append(entry)
    return results


def transition(
    project: Project, to_stage: str, user, *, step_instructions: dict[str, dict] | None = None
) -> ProjectStageTransition:
    """Mueve la operación a ``to_stage`` y lanza lo que esa etapa pide."""
    if to_stage not in STAGE_ORDER:
        raise StageError(f"Etapa desconocida: {to_stage!r}.")

    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        from_stage = current_stage(locked)
        if to_stage == from_stage:
            raise StageError(f"La operación ya está en {STAGE_LABELS[to_stage]}.")
        if not can_transition(user, locked, to_stage):
            raise StagePermissionError(
                "Volver a una etapa anterior es del staff de Ecofilia o de quien "
                "administra la institución."
                if stage_index(to_stage) < stage_index(from_stage)
                else "No tenés permiso para cambiar la etapa de esta operación."
            )

        previous_notes = dict(locked.context_notes or {})
        notes = dict(previous_notes)
        notes["estado"] = to_stage
        locked.context_notes = notes
        locked.save(update_fields=["context_notes", "updated_at"])
        record_if_changed(locked, previous_notes, user)
        record = ProjectStageTransition.objects.create(
            project=locked,
            from_stage=from_stage,
            to_stage=to_stage,
            created_by=user if getattr(user, "is_authenticated", False) else None,
        )

    project.refresh_from_db()
    # Fuera de la transacción: las corridas se encolan apenas existen.
    if stage_index(to_stage) > stage_index(from_stage):
        record.launches = launch_stage_workflows(
            project, to_stage, user, step_instructions=step_instructions
        )
        record.save(update_fields=["launches"])
    return record


def retry_pending_stage_launches(project: Project) -> list[dict] | None:
    """Reintenta lo que quedó bloqueado al entrar a la etapa actual.

    Sólo mira la última transición hacia la etapa en la que está la operación:
    lo que se bloqueó en una etapa que ya quedó atrás no se lanza. Devuelve
    ``None`` si no había nada pendiente.
    """
    stage = current_stage(project)
    record = (
        ProjectStageTransition.objects.filter(project=project, to_stage=stage)
        .order_by("-created_at", "-id")
        .first()
    )
    if record is None:
        return None
    blocked_entries = [
        entry
        for entry in (record.launches or [])
        if isinstance(entry, dict) and entry.get("status") == "blocked"
    ]
    blocked = [entry.get("skill_slug") for entry in blocked_entries]
    instrucciones = {
        entry.get("skill_slug"): entry["step_instructions"]
        for entry in blocked_entries
        if isinstance(entry.get("step_instructions"), dict)
    }
    if not blocked:
        return None

    owner = record.created_by or project.owner
    with transaction.atomic():
        # Dos documentos que terminan a la vez llegan acá juntos: el bloqueo
        # sobre el registro hace que el segundo vea lo que lanzó el primero.
        locked = ProjectStageTransition.objects.select_for_update().get(pk=record.pk)
        retried = {
            entry["skill_slug"]: entry
            for entry in launch_stage_workflows(
                project, stage, owner, slugs=blocked, step_instructions=instrucciones or None
            )
        }
        locked.launches = [
            retried.get(entry.get("skill_slug"), entry) if isinstance(entry, dict) else entry
            for entry in (locked.launches or [])
        ]
        locked.save(update_fields=["launches"])
    return list(retried.values())


def record_launches(project: Project, stage: str, launches: list[dict]) -> None:
    """Anota en la última transición hacia ``stage`` lo que se acaba de lanzar.

    Así un "Generar ahora" deja de figurar como bloqueado y el reintento
    automático no vuelve sobre lo que ya arrancó. Una operación creada
    directamente en la etapa no tiene transición: si algo quedó bloqueado (por
    ejemplo, documentos que todavía se procesan) se crea el registro de su
    entrada a la etapa, para que se reintente solo cuando terminen.
    """
    record = (
        ProjectStageTransition.objects.filter(project=project, to_stage=stage)
        .order_by("-created_at", "-id")
        .first()
    )
    if record is None:
        if any(isinstance(e, dict) and e.get("status") == "blocked" for e in launches):
            ProjectStageTransition.objects.create(
                project=project,
                from_stage="",
                to_stage=stage,
                created_by=project.owner,
                launches=launches,
            )
        return
    by_slug = {entry["skill_slug"]: entry for entry in launches if isinstance(entry, dict)}
    merged = []
    for entry in record.launches or []:
        slug = entry.get("skill_slug") if isinstance(entry, dict) else None
        merged.append(by_slug.pop(slug, entry) if slug else entry)
    merged.extend(by_slug.values())
    record.launches = merged
    record.save(update_fields=["launches"])


def on_execution_finished(execution) -> None:
    """Lo que dispara una corrida de la operación al terminar.

    El guiado completado vuelca sus secciones como borradores sobre la IET.

    Se llama al final de cada corrida y nunca levanta: un problema acá no puede
    dejar fallida una corrida que terminó bien.
    """
    from apps.skill.models import ExecutionStatus

    try:
        if execution.project_id is None or execution.status != ExecutionStatus.COMPLETED:
            return
        if execution.skill.slug == GUIADO_SLUG:
            from apps.project.services.stage_reports import apply_guided_result

            apply_guided_result(execution)
    except Exception:  # noqa: BLE001
        logger.exception("Falló el seguimiento de la corrida %s", getattr(execution, "id", None))


def stage_history(project: Project) -> list[dict]:
    """Las transiciones de la operación, para la interfaz."""
    return [
        {
            "from_stage": t.from_stage,
            "to_stage": t.to_stage,
            "created_at": t.created_at,
            "created_by_email": getattr(t.created_by, "email", None),
            "launches": t.launches or [],
        }
        for t in project.stage_transitions.select_related("created_by").order_by("created_at", "id")
    ]
