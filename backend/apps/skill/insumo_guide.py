"""
El insumo de Ecofilia como guía de su IET.

El insumo corre primero —es más corto y rápido— y la IET lo recibe como
contexto. Así lo que Ecofilia sugiere en Alineación y Enverdecimiento (que sale
del insumo) y lo que escribe en su informe no se contradicen: la IET parte de
ese análisis previo y, si los documentos la llevan a otra conclusión, lo dice.

El bloque va en el system prompt, junto al contexto de la operación (ver
``_with_operation_context`` en ``apps.skill.services``), sólo para los workflows
del informe (la IET y el guiado). Se arma una vez por corrida.
"""
from __future__ import annotations

import re

IET_SLUG = "caf-iet-datbc-evaluacion-tecnica"
GUIADO_SLUG = "caf-iet-guiado-ejecutivo"
INSUMO_SLUG = "caf-insumo-ejecutivo"

# Los workflows que reciben el insumo como guía.
GUIDED_BY_INSUMO = {IET_SLUG, GUIADO_SLUG}

_MAX_CHARS = 12_000

HEADER = (
    "## Insumo de Ecofilia (análisis previo de esta operación)\n"
    "Es el análisis previo que Ecofilia hizo de esta operación, el mismo que leyó el "
    "ejecutivo y del que salen las sugerencias de alineación y enverdecimiento. Usalo "
    "como guía: tus conclusiones tienen que ser coherentes con él. Si los documentos "
    "te llevan a una conclusión distinta, decilo explícitamente y explicá por qué."
)


def latest_insumo(project, stage: str):
    """El último insumo completado de la operación en ``stage``."""
    from apps.skill.models import ExecutionStatus, SkillExecution

    stages = [stage, ""] if stage == "ido_cci" else [stage]
    return (
        SkillExecution.objects.filter(
            project=project,
            skill__slug=INSUMO_SLUG,
            status=ExecutionStatus.COMPLETED,
            stage__in=stages,
        )
        .order_by("-finished_at", "-id")
        .first()
    )


def _steps_by_number(execution) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for step in (execution.output_structured or {}).get("steps") or []:
        match = re.match(r"\s*(\d+)\.\s", str(step.get("title") or ""))
        if match:
            out[int(match.group(1))] = step
    return out


def _text(step: dict | None) -> str:
    if not step:
        return ""
    content = str(step.get("content") or "").strip()
    lines = content.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _rows(step: dict | None) -> list[dict]:
    """Las filas de una tabla del insumo, sin las marcas de cita de sus celdas."""
    from apps.skill.citations import strip_cell_citations

    table = (step or {}).get("table") or {}
    rows = table.get("rows") if isinstance(table, dict) else None
    return [
        {k: strip_cell_citations(v) if isinstance(v, str) else v for k, v in row.items()}
        for row in rows or []
        if isinstance(row, dict)
    ]


def insumo_components(execution) -> list[dict]:
    """Los componentes que el insumo sugiere como financiamiento verde."""
    return _rows(_steps_by_number(execution).get(4)) if execution else []


def insumo_opportunities(execution) -> list[dict]:
    return _rows(_steps_by_number(execution).get(5)) if execution else []


def render_insumo(execution) -> str:
    """El insumo como texto para el prompt."""
    steps = _steps_by_number(execution)
    parts: list[str] = [HEADER]

    for number, title in (
        (8, "Lo primero a mirar"),
        (1, "Lectura de la operación"),
        (2, "Contexto país"),
        (3, "Elementos clave para el análisis de la operación"),
    ):
        text = _text(steps.get(number))
        if text:
            parts.append(f"### {title}\n{text}")

    components = _rows(steps.get(4))
    if components:
        lines = [
            f"- {row.get('componente', '')} — {row.get('objetivo', '')}"
            f"{' — ' + str(row.get('categoria')) if row.get('categoria') else ''}"
            f"{' — USD ' + str(row.get('monto_usd')) if row.get('monto_usd') not in (None, '') else ''}"
            f" (confianza {str(row.get('confianza', '')).lower()}): {row.get('justificacion', '')}"
            for row in components
        ]
        parts.append("### Componentes con potencial de financiamiento verde\n" + "\n".join(lines))

    opportunities = _rows(steps.get(5))
    if opportunities:
        lines = [
            f"- {row.get('oportunidad', '')} ({row.get('objetivo', '')}; meta: {row.get('meta_pais', '')})"
            for row in opportunities
        ]
        parts.append("### Oportunidades de enverdecimiento\n" + "\n".join(lines))

    criteria = _rows(steps.get(6)) + _rows(steps.get(7))
    if criteria:
        lines = [
            f"- {row.get('criterio', '')} [{row.get('alerta', '')}]: {row.get('puntos_clave', '')}"
            f" Falta: {row.get('informacion_faltante', '')}"
            for row in criteria
        ]
        parts.append("### Puntos clave por criterio de alineación\n" + "\n".join(lines))

    text = "\n\n".join(parts)
    return text[:_MAX_CHARS]


def build_insumo_guide_block(execution) -> str:
    """El bloque del insumo para la corrida, o vacío si no corresponde."""
    if execution.project_id is None or execution.skill.slug not in GUIDED_BY_INSUMO:
        return ""
    insumo = latest_insumo(execution.project, execution.stage or "ido_cci")
    return render_insumo(insumo) if insumo else ""
