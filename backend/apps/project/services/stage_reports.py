"""
El informe del ejecutivo de cada etapa.

Ecofilia genera su reporte al entrar a IDO; el ejecutivo arma el suyo, que es
el oficial, por una de tres vías:

- **editado**: trabaja sobre el reporte de Ecofilia. Sus ediciones son capas
  sobre la corrida (``ExecutionSectionEdit``), así que el original no se pierde.
- **guiado**: completa sus determinaciones y fundamentos en la pestaña
  Alineación —con el insumo de Ecofilia como referencia— y Ecofilia redacta el
  informe con ese criterio.
- **subido**: lo escribe afuera y lo sube. Ecofilia lo lee para obtener su
  mirada estructurada, pero no lo suma al expediente de la operación: si lo
  hiciera, las corridas de Ecofilia leerían la conclusión del ejecutivo.

Hasta emitirlo puede cambiar de vía. Emitido, queda inmutable: se guarda el
informe tal como estaba y la mirada del ejecutivo en ese momento.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from apps.project.models import (
    Project,
    ProjectDocument,
    ProjectStageReport,
    ProjectStageReportOrigin as Origin,
)
from apps.project.services.caf_criteria import build_criterio_ejecutivo, executive_assessment
from apps.project.services.stages import (
    GUIADO_SLUG,
    IET_SLUG,
    LECTURA_SLUG,
    STAGE_LABELS,
    current_stage,
    is_ecofilia_staff,
    retry_pending_stage_launches,
    runs_for_stage,
)

logger = logging.getLogger(__name__)

# Cuántas veces puede el ejecutivo generar el informe guiado en una etapa. Cada
# generación es una corrida entera del IET; el staff no tiene tope.
GUIADO_MAX_PER_STAGE = 3

# Las vías que viven en una corrida de IET sólo existen en IDO: el IET es el
# informe de IDO, y el análisis de DEC todavía no está.
_IET_STAGES = ("ido_cci",)


class StageReportError(ValueError):
    """Lo pedido no se puede hacer en el estado actual del informe."""


class StageReportPermissionError(PermissionError):
    pass


def active_report(project: Project, stage: str) -> ProjectStageReport | None:
    return (
        ProjectStageReport.objects.filter(project=project, stage=stage, voided_at__isnull=True)
        .select_related("execution", "document", "reader_execution", "emitted_by")
        .first()
    )


def _require_stage(project: Project) -> str:
    stage = current_stage(project)
    if not stage:
        raise StageReportError("La operación no tiene etapa: definila antes de armar el informe.")
    return stage


def _editable_report(project: Project, stage: str, user) -> ProjectStageReport:
    """El informe vigente de la etapa, o uno nuevo sin guardar. Nunca uno emitido."""
    report = active_report(project, stage)
    if report is not None and report.is_emitted:
        raise StageReportError(
            f"El informe de {STAGE_LABELS.get(stage, stage)} ya se emitió y no se puede cambiar."
        )
    if report is None:
        report = ProjectStageReport(project=project, stage=stage, created_by=user)
    return report


def current_ecofilia_report(project: Project, stage: str):
    """La corrida del IET que vale como reporte de Ecofilia en la etapa.

    Es la misma regla que usa la interfaz: la más reciente entre su fin y su
    ascenso, entre las que terminaron bien.
    """
    from apps.skill.models import ExecutionStatus, Skill

    skill = Skill.objects.filter(slug=IET_SLUG).first()
    if skill is None:
        return None
    runs = runs_for_stage(project, skill, stage).filter(status=ExecutionStatus.COMPLETED)
    return max(
        runs,
        key=lambda run: max(
            filter(None, (run.promoted_at, run.finished_at, run.created_at))
        ),
        default=None,
    )


def choose_editado(project: Project, user) -> ProjectStageReport:
    stage = _require_stage(project)
    if stage not in _IET_STAGES:
        raise StageReportError("En esta etapa todavía no hay reporte de Ecofilia para editar.")
    execution = current_ecofilia_report(project, stage)
    if execution is None:
        raise StageReportError("Todavía no hay un reporte de Ecofilia terminado en esta etapa.")
    report = _editable_report(project, stage, user)
    report.origin = Origin.EDITADO
    report.execution = execution
    report.document = None
    report.reader_execution = None
    report.save()
    return report


def guiado_runs_used(project: Project, stage: str) -> int:
    """Generaciones del guiado que cuentan para el tope (las que no fallaron)."""
    from apps.skill.models import ExecutionStatus, Skill

    skill = Skill.objects.filter(slug=GUIADO_SLUG).first()
    if skill is None:
        return 0
    return runs_for_stage(project, skill, stage).exclude(status=ExecutionStatus.FAILED).count()


def generate_guiado(project: Project, user) -> ProjectStageReport:
    from apps.skill.launch import launch_institutional
    from apps.skill.models import Skill

    stage = _require_stage(project)
    if stage not in _IET_STAGES:
        raise StageReportError("El informe guiado todavía no está disponible en esta etapa.")
    skill = Skill.objects.filter(slug=GUIADO_SLUG).first()
    if skill is None:
        raise StageReportError("El agente del informe guiado no está disponible.")

    criterio = build_criterio_ejecutivo(project.context_notes)
    if not criterio:
        raise StageReportError(
            "Completá tus determinaciones en Alineación antes de generar tu informe."
        )
    if not is_ecofilia_staff(user) and guiado_runs_used(project, stage) >= GUIADO_MAX_PER_STAGE:
        raise StageReportError(
            f"Ya se generaron {GUIADO_MAX_PER_STAGE} versiones guiadas en esta etapa. "
            "Seguí trabajando la última en el Editor, o pedile una nueva al staff de Ecofilia."
        )
    # Validar antes de lanzar: si el informe ya está emitido no se gasta una corrida.
    report = _editable_report(project, stage, user)

    outcome = launch_institutional(
        skill,
        owner=user,
        project=project,
        stage=stage,
        input_values={"criterio_ejecutivo": criterio},
        launched_by="guiado",
    )
    if outcome.execution is None:
        raise StageReportError(outcome.reason or "No se pudo generar el informe guiado.")

    report.origin = Origin.GUIADO
    report.execution = outcome.execution
    report.document = None
    report.reader_execution = None
    report.save()
    return report


def attach_subido(project: Project, user, document_slug: str) -> ProjectStageReport:
    from apps.document.models import Document

    stage = _require_stage(project)
    qs = Document.objects.filter(slug=document_slug)
    if not user.is_staff:
        qs = qs.filter(owner=user)
    document = qs.first()
    if document is None:
        raise StageReportError("No encontramos ese documento entre los tuyos.")
    if ProjectDocument.objects.filter(project=project, document=document).exists():
        raise StageReportError(
            "Ese documento es parte del expediente de la operación. Subí tu informe "
            "como un documento aparte: si quedara en el expediente, Ecofilia lo leería "
            "como evidencia."
        )

    report = _editable_report(project, stage, user)
    report.origin = Origin.SUBIDO
    report.document = document
    report.execution = None
    report.reader_execution = None
    report.save()
    maybe_launch_reading(report)
    return report


def maybe_launch_reading(report: ProjectStageReport):
    """Lanza la lectura del informe subido, si el documento ya se puede leer."""
    from apps.document.models import ChunkingStatus
    from apps.skill.launch import launch_institutional
    from apps.skill.models import Skill

    if report.origin != Origin.SUBIDO or report.reader_execution_id or not report.document_id:
        return None
    if report.document.chunking_status not in (ChunkingStatus.DONE, ChunkingStatus.PARTIAL):
        return None
    skill = Skill.objects.filter(slug=LECTURA_SLUG).first()
    if skill is None:
        return None

    outcome = launch_institutional(
        skill,
        owner=report.created_by or report.project.owner,
        document=report.document,
        stage=report.stage,
        launched_by="lectura",
    )
    if outcome.execution is not None:
        report.reader_execution = outcome.execution
        report.save(update_fields=["reader_execution", "updated_at"])
    return outcome


def reading_assessment(report: ProjectStageReport) -> dict:
    """Lo que la lectura sacó del informe subido, si terminó."""
    from apps.skill.models import ExecutionStatus

    run = report.reader_execution
    if run is None or run.status != ExecutionStatus.COMPLETED:
        return {}
    steps = (run.output_structured or {}).get("steps") or []
    out: dict = {}
    for step in steps:
        title = str(step.get("title") or "")
        table = step.get("table") or {}
        rows = table.get("rows") if isinstance(table, dict) else None
        if title.startswith("1.") and isinstance(rows, list):
            out["determinaciones"] = {
                str(row.get("criterio")): {
                    "determinacion": row.get("determinacion"),
                    "fundamento": row.get("fundamento"),
                }
                for row in rows
                if isinstance(row, dict) and row.get("criterio")
            }
        elif title.startswith("2.") and isinstance(rows, list):
            out["recomendaciones"] = [row for row in rows if isinstance(row, dict)]
        elif title.startswith("3."):
            out["consideraciones_adicionales"] = str(step.get("content") or "").strip()
    return out


def emit(project: Project, user, markdown: str = "") -> ProjectStageReport:
    stage = _require_stage(project)
    with transaction.atomic():
        report = (
            ProjectStageReport.objects.select_for_update()
            .filter(project=project, stage=stage, voided_at__isnull=True)
            .first()
        )
        if report is None:
            # Sin vía elegida, lo que el ejecutivo viene trabajando es el
            # reporte de Ecofilia con sus ediciones: es lo que se emite.
            report = choose_editado(project, user)
            report = ProjectStageReport.objects.select_for_update().get(pk=report.pk)
        if report.is_emitted:
            raise StageReportError("Este informe ya se emitió.")

        assessment = executive_assessment(project.context_notes)
        if report.origin == Origin.SUBIDO:
            if not report.document_id:
                raise StageReportError("Falta el documento del informe.")
            assessment["lectura"] = reading_assessment(report)
            report.emitted_markdown = ""
        else:
            if not (markdown or "").strip():
                raise StageReportError("Falta el contenido del informe a emitir.")
            report.emitted_markdown = markdown

        report.emitted_assessment = assessment
        report.emitted_at = timezone.now()
        report.emitted_by = user
        report.save()
    return report


def void(report: ProjectStageReport, user, reason: str = "") -> ProjectStageReport:
    if not is_ecofilia_staff(user):
        raise StageReportPermissionError("Anular un informe es del staff de Ecofilia.")
    if report.voided_at is not None:
        raise StageReportError("Este informe ya está anulado.")
    report.voided_at = timezone.now()
    report.voided_by = user
    report.void_reason = (reason or "").strip()[:500]
    report.save(update_fields=["voided_at", "voided_by", "void_reason", "updated_at"])
    return report


def is_emitted_document(document) -> bool:
    """Si ``document`` es el informe oficial emitido de alguna operación."""
    return ProjectStageReport.objects.filter(
        document=document, emitted_at__isnull=False, voided_at__isnull=True
    ).exists()


def run_followups_for_document(document_id: int) -> None:
    """Lo que habilita un documento que terminó de procesarse.

    - Si es el informe subido de un ejecutivo, se lanza su lectura.
    - Si es parte del expediente de una operación con lanzamientos bloqueados
      en su etapa, se reintentan. El documento principal no se mira acá: su
      reintento corre al final del autocompletado de objetivo y componentes,
      para que el análisis arranque con esos datos ya cargados.

    Nunca levanta: se llama al final de la ingesta.
    """
    try:
        for report in ProjectStageReport.objects.filter(
            document_id=document_id,
            voided_at__isnull=True,
            reader_execution__isnull=True,
        ).select_related("document", "project"):
            maybe_launch_reading(report)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo lanzar la lectura del informe del documento %s", document_id)

    try:
        projects = Project.objects.filter(project_documents__document_id=document_id).exclude(
            blueprint_document_id=document_id
        )
        for project in projects:
            retry_pending_stage_launches(project)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudieron reintentar lanzamientos tras el documento %s", document_id)
