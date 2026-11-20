"""
El resumen por etapa de las operaciones de una institución, para el tablero.

El tablero muestra lo que cargaron los ejecutivos, no lo que dijo la IA. Por
cada operación y cada etapa con informe (en CAF, IDO y DEC) dice:

- **informe**: el estado del informe del ejecutivo (sin informe, borrador o
  emitido), su versión, su vía y cuándo se emitió;
- **mirada**: la determinación y el enverdecimiento del ejecutivo en esa etapa.
  Sale de lo emitido; si la etapa todavía no se emitió, de lo congelado al
  salir de ella (``por_etapa``) o, si es la etapa actual, de lo que viene
  cargando. ``fuente`` dice cuál;
- **ia** (sólo staff): el estado del análisis de Ecofilia de la etapa, para
  la comparativa.

Es liviano a propósito: nada de markdown ni de pasos de corridas. Un listado
con el informe completo de cada operación tumbó la API (ver la nota de OOM).
"""
from __future__ import annotations

from collections import defaultdict

from apps.project.models import Project, ProjectStageReport
from apps.project.services.caf_criteria import executive_assessment
from apps.project.services.stages import POR_ETAPA_KEY, current_stage
from apps.user import institution_config

SIN_INFORME = "sin_informe"
BORRADOR = "borrador"
EMITIDO = "emitido"


def _mirada(assessment: dict | None, fuente: str) -> dict | None:
    if not isinstance(assessment, dict):
        return None
    determinaciones = assessment.get("determinaciones") or {}
    enverdecimiento = assessment.get("enverdecimiento") or {}
    if not determinaciones and not enverdecimiento:
        return None
    return {
        "fuente": fuente,
        "determinaciones": determinaciones,
        "enverdecimiento": enverdecimiento,
    }


def _ia_por_operacion(projects: list[Project]) -> dict[tuple[int, str], dict]:
    from apps.skill.models import ExecutionStatus, SkillExecution

    slugs_por_operacion: dict[int, dict[str, str]] = {}
    todos: set[str] = set()
    for project in projects:
        config = institution_config.config_for_project(project)
        por_etapa = {}
        for stage in institution_config.report_stages(config):
            slug = institution_config.report_workflow(config, stage)
            if slug:
                por_etapa[stage] = slug
                todos.add(slug)
        slugs_por_operacion[project.pk] = por_etapa
    if not todos:
        return {}
    filas = (
        SkillExecution.objects.filter(project_id__in=[p.pk for p in projects], skill__slug__in=todos)
        .values("project_id", "skill__slug", "stage", "status", "finished_at", "created_at")
        .order_by("created_at")
    )
    out: dict[tuple[int, str], dict] = {}
    for fila in filas:
        etapas = slugs_por_operacion.get(fila["project_id"]) or {}
        stage = fila["stage"] or "ido_cci"
        if etapas.get(stage) != fila["skill__slug"]:
            continue
        previo = out.get((fila["project_id"], stage))
        estado = fila["status"]
        # Una corrida terminada cuenta aunque después haya otra fallida.
        if previo and previo["estado"] == ExecutionStatus.COMPLETED and estado == ExecutionStatus.FAILED:
            continue
        out[(fila["project_id"], stage)] = {
            "estado": estado,
            "fecha": (fila["finished_at"] or fila["created_at"]).isoformat(),
        }
    return out


def summary_for(projects, *, staff: bool) -> list[dict]:
    projects = list(projects)
    reports: dict[tuple[int, str], list[ProjectStageReport]] = defaultdict(list)
    for report in ProjectStageReport.objects.filter(
        project_id__in=[p.pk for p in projects], voided_at__isnull=True
    ).order_by("id"):
        reports[(report.project_id, report.stage)].append(report)
    ia = _ia_por_operacion(projects) if staff else {}

    out: list[dict] = []
    for project in projects:
        config = institution_config.config_for_project(project)
        stages = institution_config.report_stages(config)
        if not stages:
            continue
        notes = project.context_notes if isinstance(project.context_notes, dict) else {}
        actual = current_stage(project)
        congeladas = notes.get(POR_ETAPA_KEY) or {}
        etapas = []
        ultima = project.updated_at
        for stage in stages:
            propios = reports.get((project.pk, stage), [])
            vigente = next((r for r in reversed(propios) if r.superseded_at is None), None)
            emitido = next((r for r in reversed(propios) if r.emitted_at is not None), None)
            if vigente is None:
                informe = {"estado": SIN_INFORME}
            else:
                informe = {
                    "estado": EMITIDO if vigente.emitted_at else BORRADOR,
                    "report_id": vigente.pk,
                    "origen": vigente.origin,
                    "version": len(propios),
                    "emitted_at": vigente.emitted_at.isoformat() if vigente.emitted_at else None,
                }
            if emitido is not None and emitido.emitted_at and (ultima is None or emitido.emitted_at > ultima):
                ultima = emitido.emitted_at

            mirada = None
            if vigente is not None and vigente.emitted_at:
                mirada = _mirada(vigente.emitted_assessment, "emitido")
            if mirada is None and isinstance(congeladas.get(stage), dict):
                mirada = _mirada(congeladas[stage].get("assessment"), "congelado")
            if mirada is None and stage == actual:
                mirada = _mirada(executive_assessment(notes), "vigente")
            if mirada is None and emitido is not None:
                mirada = _mirada(emitido.emitted_assessment, "emitido")

            entrada = {
                "stage": stage,
                "label": institution_config.stage_label(config, stage),
                "informe": informe,
                "mirada": mirada,
            }
            if staff:
                entrada["ia"] = ia.get((project.pk, stage)) or {"estado": "sin_correr"}
            etapas.append(entrada)
        out.append(
            {
                "project_slug": project.slug,
                "estado": actual,
                "etapas": etapas,
                "ultima_actividad": ultima.isoformat() if ultima else None,
            }
        )
    return out
