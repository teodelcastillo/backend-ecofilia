"""
El registro de evaluación: la misma forma para toda evaluación de alineación.

Una evaluación puede venir de tres lados y el tablero las necesita iguales para
contarlas y compararlas:

- ``ia``: el análisis del Asistente IA (la IET vigente de la etapa), leído de su
  síntesis por criterio (``S.1``) y de sus componentes verdes (``S.2``).
- ``ejecutivo``: el informe emitido del ejecutivo armado en el portal (guiado o
  editado), con la mirada que tenía al emitirlo (``emitted_assessment``).
- ``externo``: el informe que el ejecutivo escribió afuera y subió —también los
  IET históricos que carga el staff—, con lo que leyó de él la lectura.

Cada registro trae los criterios de la institución de la operación
(``evaluation.criteria`` de su configuración; en CAF, los ocho CT y los dos
objetivos) con ``valor`` y ``fundamento``. Una institución sin criterios no
tiene registro. Lo que la fuente no dice queda en ``NO_CONSTA``, no vacío, para
poder contar qué tan completos vienen los informes hechos por fuera.

No se guarda: se arma al pedirlo, así nunca queda desfasado de lo que cambió.
"""
from __future__ import annotations

import re

from apps.project.models import Project, ProjectStageReport, ProjectStageReportOrigin as Origin
from apps.project.services.caf_criteria import (
    ecofilia_determinations,
    ecofilia_green_components,
)
from apps.user import institution_config

ORIGEN_IA = "ia"
ORIGEN_EJECUTIVO = "ejecutivo"
ORIGEN_EXTERNO = "externo"

NO_CONSTA = "No consta"
# Marca de las operaciones creadas por la carga de un IET histórico.
HISTORICO_KEY = "historico"


def criterio_keys(project: Project) -> list[str]:
    """Las claves de los criterios de la institución de la operación, en orden."""
    config = institution_config.config_for_project(project)
    return [c["key"] for c in institution_config.criteria(config)]


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def key_de_etiqueta(label: str) -> str | None:
    """«CT A1 — …» → ``ct_a1``; «Objetivo 2 — …» → ``objetivo_2``."""
    ct = re.match(r"\s*CT ([AM])(\d)", label or "")
    if ct:
        return f"ct_{ct.group(1).lower()}{ct.group(2)}"
    objetivo = re.match(r"\s*Objetivo (\d)", label or "")
    return f"objetivo_{objetivo.group(1)}" if objetivo else None


def _criterios(valores: dict[str, dict], keys: list[str]) -> dict[str, dict]:
    """Los criterios de la institución, en orden; lo que falta, «No consta»."""
    out = {}
    for key in keys:
        item = valores.get(key) or {}
        valor = _text(item.get("valor"))
        out[key] = {
            "valor": valor if valor and not valor.lower().startswith("no consta") else NO_CONSTA,
            "fundamento": _text(item.get("fundamento")),
        }
    return out


def _anio(project: Project) -> int | None:
    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    if isinstance(notes.get("year"), int):
        return notes["year"]
    fecha = _text(notes.get("fecha_informe"))
    match = re.match(r"(\d{4})-", fecha)
    if match:
        return int(match.group(1))
    return project.created_at.year if project.created_at else None


def _base(project: Project, stage: str) -> dict:
    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    return {
        "project_slug": project.slug,
        "project_name": project.name,
        "pais": _text(notes.get("pais")),
        "tipo_operacion": _text(notes.get("tipo_operacion")),
        "monto": _text(notes.get("monto")),
        "anio": _anio(project),
        "stage": stage,
        "historico": bool(notes.get(HISTORICO_KEY)),
    }


def _registro_ia(project: Project, stage: str, keys: list[str]) -> dict | None:
    from apps.project.services.stage_reports import current_ecofilia_report

    execution = current_ecofilia_report(project, stage)
    if execution is None:
        return None
    determinaciones = ecofilia_determinations(execution)
    if not determinaciones:
        return None
    return {
        **_base(project, stage),
        "origen": ORIGEN_IA,
        "fecha": (execution.finished_at or execution.created_at).isoformat(),
        "criterios": _criterios(
            {
                key: {"valor": item.get("determinacion"), "fundamento": item.get("fundamento")}
                for key, item in determinaciones.items()
            },
            keys,
        ),
        "actividades_elegibles": len(ecofilia_green_components(execution)),
    }


def _registro_ejecutivo(project: Project, stage: str, keys: list[str]) -> dict | None:
    """El último informe emitido de la etapa o, en un histórico, el subido aunque
    no se haya emitido: lo que vale es lo que dice el documento."""
    from apps.project.services.stage_reports import reading_assessment

    reports = ProjectStageReport.objects.filter(
        project=project, stage=stage, voided_at__isnull=True
    ).select_related("reader_execution")
    historico = bool((project.context_notes or {}).get(HISTORICO_KEY))
    report = (
        reports.filter(emitted_at__isnull=False).order_by("-emitted_at", "-id").first()
        or (reports.filter(origin=Origin.SUBIDO).order_by("-id").first() if historico else None)
    )
    if report is None:
        return None

    if report.origin == Origin.SUBIDO:
        lectura = (report.emitted_assessment or {}).get("lectura") or reading_assessment(report)
        valores = {}
        for label, item in (lectura.get("determinaciones") or {}).items():
            key = key_de_etiqueta(label)
            if key and isinstance(item, dict):
                valores[key] = {"valor": item.get("determinacion"), "fundamento": item.get("fundamento")}
        origen = ORIGEN_EXTERNO
        leido = bool(lectura)
    else:
        assessment = report.emitted_assessment or {}
        determinaciones = assessment.get("determinaciones") or {}
        fundamentos = assessment.get("fundamentos") or {}
        valores = {
            key: {"valor": determinaciones.get(key), "fundamento": fundamentos.get(key)}
            for key in keys
        }
        origen = ORIGEN_EJECUTIVO
        leido = True

    return {
        **_base(project, stage),
        "origen": origen,
        "informe_origen": report.origin,
        "fecha": (report.emitted_at or report.created_at).isoformat(),
        "emitido": report.is_emitted,
        # Un subido cuya lectura todavía no terminó: no hay qué contar aún.
        "leido": leido,
        "criterios": _criterios(valores, keys),
    }


def evaluation_stages(project: Project) -> list[str]:
    """Las etapas que se evalúan: las que tienen documento principal.

    En CAF, IDO y DEC (``stages[].principal_tag``): son las etapas en las que
    hay un informe que dice algo sobre la operación.
    """
    config = institution_config.config_for_project(project)
    return [s["slug"] for s in config.get("stages") or [] if s.get("principal_tag")]


def records_for(projects) -> list[dict]:
    out: list[dict] = []
    for project in projects:
        keys = criterio_keys(project)
        if not keys:
            continue
        for stage in evaluation_stages(project):
            for registro in (
                _registro_ia(project, stage, keys),
                _registro_ejecutivo(project, stage, keys),
            ):
                if registro is not None:
                    out.append(registro)
    return out


def create_historical(*, user, organization, document, name: str, notes: dict) -> ProjectStageReport:
    """Una operación para un IET histórico, con el informe como subido.

    No se lanza el análisis del Asistente IA: el IET histórico se lee (la
    lectura arranca sola cuando el documento termina de procesarse) y entra al
    tablero como evaluación externa. El documento no se vincula al expediente:
    la operación no tiene más fuentes que el informe mismo.
    """
    from apps.project.services.stage_reports import maybe_launch_reading

    project = Project.objects.create(
        owner=user,
        organization=organization,
        name=name,
        context_notes={**notes, "estado": "ido_cci", HISTORICO_KEY: True},
    )
    report = ProjectStageReport.objects.create(
        project=project,
        stage="ido_cci",
        origin=Origin.SUBIDO,
        document=document,
        created_by=user,
    )
    maybe_launch_reading(report)
    return report
