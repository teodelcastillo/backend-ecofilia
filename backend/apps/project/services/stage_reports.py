"""
El informe del ejecutivo de cada etapa.

Ecofilia genera su reporte al entrar a IDO; el ejecutivo arma el suyo, que es
el oficial, por una de tres vías:

- **editado**: trabaja sobre el reporte de Ecofilia. Sus ediciones son capas
  sobre la corrida (``ExecutionSectionEdit``), así que el original no se pierde.
- **guiado**: completa paso a paso sus determinaciones, fundamentos y
  enverdecimiento —con la ficha de la IET de Ecofilia como referencia— y Ecofilia
  reescribe con ese criterio sólo las secciones de enverdecimiento y alineación
  (C, D y E) donde difiere de la suya. Lo reescrito entra como borradores sobre
  el reporte de Ecofilia, que el ejecutivo revisa y publica en el Editor.
- **subido**: lo escribe afuera y lo sube. Ecofilia lo lee para obtener su
  mirada estructurada, pero no lo suma al expediente de la operación: si lo
  hiciera, las corridas de Ecofilia leerían la conclusión del ejecutivo.

Hasta emitirlo puede cambiar de vía. Emitido, queda inmutable: se guarda el
informe tal como estaba y la mirada del ejecutivo en ese momento.
"""
from __future__ import annotations

import logging
import re

from django.db import transaction
from django.utils import timezone

from apps.project.models import (
    Project,
    ProjectDocument,
    ProjectStageReport,
    ProjectStageReportOrigin as Origin,
)
from apps.project.services.caf_criteria import (
    build_criterio_ejecutivo,
    ecofilia_determinations,
    ecofilia_green_components,
    executive_assessment,
    guided_basis,
)
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
        .select_related(
            "execution", "guided_execution", "document", "reader_execution", "emitted_by"
        )
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
    """Lanza el guiado sobre el reporte de Ecofilia de la etapa.

    El informe queda ``guiado`` con ``execution`` = el reporte de Ecofilia (es el
    documento que se trabaja y se emite) y ``guided_execution`` = la corrida
    guiada. Cuando ésta termina, ``apply_guided_result`` vuelca como borradores
    las secciones que cambiaron.
    """
    from apps.skill.launch import launch_institutional
    from apps.skill.models import Skill

    stage = _require_stage(project)
    if stage not in _IET_STAGES:
        raise StageReportError("El informe guiado todavía no está disponible en esta etapa.")
    skill = Skill.objects.filter(slug=GUIADO_SLUG).first()
    if skill is None:
        raise StageReportError("El agente del informe guiado no está disponible.")
    base = current_ecofilia_report(project, stage)
    if base is None:
        raise StageReportError(
            "Todavía no hay un reporte de Ecofilia terminado en esta etapa: el informe "
            "guiado se escribe sobre él."
        )

    criterio = build_criterio_ejecutivo(
        project.context_notes,
        ecofilia=ecofilia_determinations(base),
        componentes_ecofilia=ecofilia_green_components(base),
    )
    if not criterio:
        raise StageReportError(
            "Completá tus determinaciones o tu enverdecimiento antes de generar tu informe."
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
    if outcome.status == "failed":
        # No llegó a la cola: la vía del informe no cambia, y la corrida
        # fallida no cuenta para el tope, así que se puede volver a intentar.
        raise StageReportError(
            "No se pudo poner en marcha la generación. Volvé a intentarlo en unos minutos."
        )

    report.origin = Origin.GUIADO
    report.execution = base
    report.guided_execution = outcome.execution
    # Con qué criterio se generó: si el ejecutivo lo cambia después, los
    # borradores ya no lo reflejan y la interfaz lo avisa.
    report.guided_result = {"base": guided_basis(project.context_notes)}
    report.document = None
    report.reader_execution = None
    report.save()
    return report


SIN_CAMBIOS = "sin cambios"
_CODE = re.compile(r"^\s*([A-Z]\.\d+(?:\.\d+)?)\.?\s")


def _norm_title(title) -> str:
    return " ".join(str(title or "").lower().split())


def _code(title) -> str:
    match = _CODE.match(str(title or ""))
    return match.group(1) if match else ""


def _is_sin_cambios(content: str) -> bool:
    letters = re.sub(r"[^a-záéíóúñ ]", " ", content.lower())
    letters = " ".join(letters.split())
    return letters.startswith(SIN_CAMBIOS) and len(content) < 200


def _strip_heading(content: str) -> str:
    lines = content.strip().splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _sources_footer(citations) -> str:
    """Las fuentes que citó el paso guiado, al pie de la sección: documento y página."""
    pages: dict[str, list[str]] = {}
    for citation in citations or []:
        if not isinstance(citation, dict):
            continue
        name = str(citation.get("document_name") or "").strip()
        if not name:
            continue
        start, end = citation.get("page_start"), citation.get("page_end")
        page = ""
        if start:
            page = f"{start}" if not end or end == start else f"{start}–{end}"
        bucket = pages.setdefault(name, [])
        if page and page not in bucket:
            bucket.append(page)
    if not pages:
        return ""
    parts = [f"{name} (p. {', '.join(found)})" if found else name for name, found in pages.items()]
    return "**Fuentes:** " + "; ".join(parts)


def apply_guided_result(run) -> dict | None:
    """Vuelca como borradores, sobre el reporte de Ecofilia, lo que el guiado cambió.

    Cada paso del guiado tiene el título de la sección de la IET que reescribe.
    Los que dicen «SIN CAMBIOS» no tocan nada. Los demás quedan como borrador de
    esa sección, con sus fuentes al pie: el ejecutivo los revisa y publica en el
    Editor, y lo que escribió Ecofilia sigue intacto debajo.

    Devuelve el resumen guardado en ``guided_result``, o ``None`` si no había
    nada que aplicar (el informe cambió de vía, se emitió o se anuló).
    """
    from apps.skill.report_edits import save_section_draft
    from apps.skill.models import ExecutionSectionEdit

    report = (
        ProjectStageReport.objects.filter(guided_execution=run, voided_at__isnull=True)
        .select_related("execution")
        .first()
    )
    if report is None or report.is_emitted or report.execution is None:
        return None
    if report.guided_result.get("aplicado_en"):
        return report.guided_result

    base_steps = [
        step
        for step in (report.execution.output_structured or {}).get("steps") or []
        if isinstance(step, dict) and isinstance(step.get("step_id"), int)
    ]
    by_title = {_norm_title(step.get("title")): step for step in base_steps}
    # Si el título cambió (p. ej. se afinó en el Studio), se busca por código,
    # pero sólo si el código es único: la IET tiene varios "D.2." (un paso por
    # criterio) y adivinar entre ellos volcaría el texto en otra sección.
    codes = [_code(step.get("title")) for step in base_steps]
    by_code = {
        code: step for code, step in zip(codes, base_steps) if code and codes.count(code) == 1
    }
    author = run.owner or report.created_by

    cambiadas: list[dict] = []
    sin_cambios: list[str] = []
    sin_destino: list[str] = []
    reemplazadas: list[str] = []
    for step in (run.output_structured or {}).get("steps") or []:
        if not isinstance(step, dict):
            continue
        title = str(step.get("title") or "")
        content = str(step.get("content") or "").strip()
        if not content or _is_sin_cambios(content):
            sin_cambios.append(title)
            continue
        target = by_title.get(_norm_title(title)) or by_code.get(_code(title))
        if target is None:
            sin_destino.append(title)
            continue
        body = _strip_heading(content)
        footer = _sources_footer(step.get("citations"))
        if footer:
            body = f"{body}\n\n{footer}"
        existing = ExecutionSectionEdit.objects.filter(
            execution=report.execution, step_id=target["step_id"]
        ).first()
        if existing is not None and existing.has_unpublished_changes:
            reemplazadas.append(title)
        save_section_draft(report.execution, target["step_id"], body, author)
        cambiadas.append({"titulo": target.get("title") or title, "step_id": target["step_id"]})

    report.guided_result = {
        **({"base": report.guided_result["base"]} if report.guided_result.get("base") else {}),
        "cambiadas": cambiadas,
        "sin_cambios": sin_cambios,
        "sin_destino": sin_destino,
        "borradores_reemplazados": reemplazadas,
        "aplicado_en": timezone.now().isoformat(),
    }
    report.save(update_fields=["guided_result", "updated_at"])
    return report.guided_result


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
    report.guided_execution = None
    report.guided_result = {}
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
