"""
El contexto de una operación para el chat de Investigar.

El chat de una operación respondía sólo con sus documentos y con lo que el
portal había volcado en el prompt al abrir la sesión (las notas crudas, sin
decir de dónde salía cada una, y congeladas desde ese momento). Ahora cada
mensaje recibe, recién leído de la base:

- ``[Operación]``: los datos generales de la operación.
- ``[Ejecutivo]``: lo que determinó el ejecutivo (alineación por criterio con
  sus fundamentos, financiamiento verde, recomendaciones que mantiene o
  descarta, consideraciones) y el estado de su informe. Es la decisión de la
  operación.
- ``[Ecofilia]``: lo que sugiere el informe de Ecofilia de la etapa, aunque el
  ejecutivo no lo vea: la conclusión por criterio, las actividades con
  potencial de financiamiento verde y las recomendaciones. Es una sugerencia.

Cada bloque lleva su etiqueta y el chat la cita tal cual, para que se sepa de
dónde sale cada afirmación y con qué peso decirla: lo del ejecutivo, con
determinación; lo de Ecofilia, como sugerencia o potencial. Los documentos se
siguen citando con ``[#N]``.

Sólo para operaciones cuya institución tiene criterios de evaluación
configurados (hoy, CAF). Nunca levanta: si algo falla, el chat sigue sin este
bloque.
"""
from __future__ import annotations

import logging
import re

from apps.project.models import Project
from apps.project.services import caf_criteria
from apps.user import institution_config

logger = logging.getLogger(__name__)

ORIGEN_OPERACION = "[Operación]"
ORIGEN_EJECUTIVO = "[Ejecutivo]"
ORIGEN_ECOFILIA = "[Ecofilia]"
ORIGENES = (ORIGEN_OPERACION, ORIGEN_EJECUTIVO, ORIGEN_ECOFILIA)

# Las claves de `context_notes` que son la mirada del ejecutivo o datos
# internos: no van entre los datos generales.
_CLAVES_NO_GENERALES = {
    caf_criteria.ALINEACION_KEY,
    caf_criteria.FUNDAMENTOS_KEY,
    caf_criteria.INSUMO_KEY,
    caf_criteria.CONSIDERACIONES_KEY,
    caf_criteria.ENVERDECIMIENTO_KEY,
    "motivos_diferencia",
    "elementos_clave_leidos",
}

_ROTULOS_GENERALES = {
    "pais": "País",
    "monto": "Monto CAF (millones de USD)",
    "estado": "Etapa",
    "objetivo": "Objetivo de la operación",
    "componentes": "Componentes",
    "tipo_operacion": "Tipo de operación",
    "sector": "Sector",
    "ejecutivo_datbc": "Ejecutivo DATBC",
    "fecha_informe": "Fecha del informe",
    "ecosistemas_estrategicos": "Ecosistemas estratégicos",
}

INSTRUCCIONES = (
    "Además de los documentos, tenés el contexto de la operación de abajo. "
    "Cada dato lleva su origen entre corchetes; cuando lo uses, citá ese origen "
    "tal cual en la misma oración: [Operación], [Ejecutivo] o [Ecofilia]. Los "
    "fragmentos de documentos se siguen citando con [#N].\n"
    "Cómo responder según el origen:\n"
    "- [Ejecutivo] es la decisión de la operación: lo que determinó el ejecutivo "
    "de CAF. Afirmalo con determinación (por ejemplo: «según la determinación del "
    "ejecutivo, el Objetivo 1 está con brechas por el CT A1 [Ejecutivo]»).\n"
    "- [Ecofilia] es el análisis de Ecofilia: sugiere, no decide. Decilo en "
    "potencial o como sugerencia (por ejemplo: «el análisis de Ecofilia sugiere "
    "que…», «podría calificar como…») y nunca como la decisión de la operación.\n"
    "- Si el ejecutivo y Ecofilia difieren, decí las dos y aclará que vale la del "
    "ejecutivo. Si el ejecutivo todavía no determinó algo, no lo presentes como "
    "decidido: decí que está pendiente y, si la hay, cuál es la sugerencia de "
    "Ecofilia.\n"
    "- [Operación] son los datos generales cargados en la operación.\n"
    "- Este contexto está actualizado a este mensaje: si algo de la conversación "
    "anterior o de las instrucciones iniciales dice otra cosa, vale este."
)


def _texto(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _corto(texto: str, maximo: int = 420) -> str:
    texto = " ".join((texto or "").split())
    return texto if len(texto) <= maximo else texto[: maximo - 1].rsplit(" ", 1)[0] + "…"


def _valor(value) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "; ".join(_valor(v) for v in value if _valor(v))
    if isinstance(value, dict):
        partes = [f"{k}: {_valor(v)}" for k, v in value.items() if _valor(v)]
        return "; ".join(partes)
    return ""


def _usd(value) -> str:
    try:
        return f"USD {float(value):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return "monto sin desagregar"


# ── [Operación] ──────────────────────────────────────────────────────────


def _datos_generales(project: Project) -> list[str]:
    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    lineas = [f"- Nombre: {project.name}"]
    if _texto(project.description):
        lineas.append(f"- Descripción: {_corto(project.description, 600)}")
    for clave, rotulo in _ROTULOS_GENERALES.items():
        valor = _valor(notes.get(clave))
        if valor:
            lineas.append(f"- {rotulo}: {_corto(valor, 600)}")
    for clave, value in notes.items():
        if clave in _ROTULOS_GENERALES or clave in _CLAVES_NO_GENERALES or clave.startswith("_"):
            continue
        valor = _valor(value)
        if valor and len(valor) <= 600:
            lineas.append(f"- {clave.replace('_', ' ').capitalize()}: {valor}")
    return lineas


# ── [Ejecutivo] ──────────────────────────────────────────────────────────


def _estado_del_informe(project: Project, stage: str) -> str:
    from apps.project.services.stage_reports import active_report

    try:
        report = active_report(project, stage) if stage else None
    except Exception:  # noqa: BLE001
        report = None
    if report is None:
        return "El ejecutivo todavía no armó su informe de esta etapa."
    if report.is_emitted:
        return "El informe del ejecutivo de esta etapa está emitido: es definitivo."
    return "El informe del ejecutivo de esta etapa está en borrador: puede cambiar hasta emitirse."


def _mirada_del_ejecutivo(project: Project, stage: str) -> list[str]:
    assessment = caf_criteria.executive_assessment(project.context_notes)
    determinaciones = assessment["determinaciones"]
    fundamentos = assessment["fundamentos"]
    lineas = [_estado_del_informe(project, stage)]

    criterios = [
        f"- {etiqueta}: {determinaciones[clave]}."
        + (f" Fundamento: {_corto(fundamentos[clave])}" if fundamentos.get(clave) else "")
        for clave, etiqueta in caf_criteria.CRITERIOS
        if determinaciones.get(clave)
    ]
    if criterios:
        lineas.append("Alineación con el Acuerdo de París, por criterio (los objetivos salen de sus CT con las reglas de la DATBC):")
        lineas.extend(criterios)
    else:
        lineas.append("Todavía no determinó la alineación con el Acuerdo de París.")

    env = assessment["enverdecimiento"]
    componentes = env.get("componentes") or []
    incluidos = [c for c in componentes if _texto(c.get("estado")) != "descartado"]
    descartados = [c for c in componentes if _texto(c.get("estado")) == "descartado"]
    if incluidos:
        lineas.append("Actividades que cuenta como financiamiento verde:")
        for c in incluidos:
            linea = f"- {_texto(c.get('componente'))} — {_texto(c.get('objetivo')) or 'sin objetivo'}"
            if _texto(c.get("categoria")):
                linea += f" — {_texto(c.get('categoria'))}"
            linea += f" — {_usd(c.get('monto_usd'))}."
            if _texto(c.get("fundamento")):
                linea += f" Fundamento: {_corto(c.get('fundamento'))}"
            lineas.append(linea)
    elif not componentes:
        lineas.append("Todavía no confirmó qué actividades cuenta como financiamiento verde.")
    for c in descartados:
        lineas.append(f"- Descartó como verde: {_texto(c.get('componente'))}.")

    oportunidades = env.get("oportunidades") or []
    recomendadas = [o for o in oportunidades if _texto(o.get("estado")) != "descartar" and _texto(o.get("oportunidad"))]
    descartadas = [o for o in oportunidades if _texto(o.get("estado")) == "descartar" and _texto(o.get("oportunidad"))]
    if recomendadas:
        lineas.append("Recomendaciones que mantiene en su informe:")
        lineas.extend(f"- {_corto(o.get('oportunidad'))}" for o in recomendadas)
    if descartadas:
        lineas.append("Recomendaciones del informe de Ecofilia que descartó:")
        lineas.extend(f"- {_corto(o.get('oportunidad'))}" for o in descartadas)
    if assessment["consideraciones_adicionales"]:
        lineas.append(f"Consideraciones adicionales: {_corto(assessment['consideraciones_adicionales'], 800)}")
    return lineas


# ── [Ecofilia] ───────────────────────────────────────────────────────────


def _informe_de_ecofilia(project: Project):
    """La corrida del informe de Ecofilia que vale: la de la etapa actual o, si
    la etapa no tiene informe, la de la última etapa anterior que lo tuvo."""
    from apps.project.services.stage_reports import _report_stages, current_ecofilia_report
    from apps.project.services.stages import current_stage, stage_order

    actual = current_stage(project)
    con_informe = set(_report_stages(project))
    orden = stage_order(project)
    candidatas = orden[: orden.index(actual) + 1] if actual in orden else orden
    for stage in reversed(candidatas):
        if stage in con_informe:
            run = current_ecofilia_report(project, stage)
            if run is not None:
                return stage, run
    return None, None


_CODIGO_SECCION = re.compile(r"^\s*[A-Z](\.\d+)+\.?\s*")


def _mirada_de_ecofilia(project: Project) -> tuple[list[str], str]:
    stage, run = _informe_de_ecofilia(project)
    if run is None:
        return ["Todavía no hay informe de Ecofilia para esta operación."], ""
    lineas: list[str] = []
    determinaciones = caf_criteria.ecofilia_determinations(run)
    if determinaciones:
        lineas.append("Conclusión del análisis por criterio (los objetivos, con las reglas de la DATBC):")
        for clave, etiqueta in caf_criteria.CRITERIOS:
            item = determinaciones.get(clave)
            if not item:
                continue
            linea = f"- {etiqueta}: {item['determinacion']}."
            if item.get("fundamento"):
                linea += f" Por qué: {_corto(item['fundamento'])}"
            if item.get("informacion_faltante"):
                linea += f" Falta para confirmarlo: {_corto(item['informacion_faltante'], 240)}"
            lineas.append(linea)
    actividades = caf_criteria.ecofilia_green_components(run)
    if actividades:
        lineas.append("Actividades con potencial de financiamiento verde según el análisis:")
        for a in actividades:
            linea = f"- {_texto(a.get('componente'))}"
            actividad = " ".join(filter(None, (_texto(a.get("codigo_actividad")), _texto(a.get("actividad_elegible")))))
            if actividad:
                linea += f": {actividad}"
            if _texto(a.get("objetivo")):
                linea += f" ({_texto(a.get('objetivo'))})"
            linea += f" — {_usd(a.get('monto_usd'))}."
            lineas.append(linea)
    for grupo in caf_criteria.ecofilia_recommendations(run):
        titulo = _CODIGO_SECCION.sub("", grupo["titulo"]) or "Recomendaciones"
        lineas.append(f"{titulo}:")
        lineas.extend(f"- {_corto(item)}" for item in grupo["items"])
    if not lineas:
        lineas.append("El informe de Ecofilia no tiene conclusiones estructuradas para mostrar.")
    return lineas, stage


# ── El bloque ────────────────────────────────────────────────────────────


def _es_operacion_con_criterios(project: Project) -> bool:
    """De una institución con criterios de evaluación y con informe de Ecofilia
    en alguna etapa (hoy, CAF). La configuración base no tiene etapas."""
    if project.organization_id is None:
        return False
    config = institution_config.config_for_project(project)
    return bool(institution_config.criteria(config)) and bool(institution_config.report_stages(config))


def operation_context(project: Project | None) -> str:
    """El contexto de la operación para el chat, con el origen de cada dato.
    Vacío si la operación no es de una institución con criterios o si falla."""
    if project is None:
        return ""
    try:
        if not _es_operacion_con_criterios(project):
            return ""
        from apps.project.services.stages import current_stage

        stage = current_stage(project)
        ecofilia, stage_ecofilia = _mirada_de_ecofilia(project)
        partes = [
            INSTRUCCIONES,
            "CONTEXTO DE LA OPERACIÓN",
            f"{ORIGEN_OPERACION} Datos generales:\n" + "\n".join(_datos_generales(project)),
            f"{ORIGEN_EJECUTIVO} Lo que determinó el ejecutivo (la decisión de la operación):\n"
            + "\n".join(_mirada_del_ejecutivo(project, stage)),
            f"{ORIGEN_ECOFILIA} Lo que sugiere el informe de Ecofilia"
            + (f" de la etapa {stage_ecofilia}" if stage_ecofilia else "")
            + " (una sugerencia, no la decisión):\n"
            + "\n".join(ecofilia),
        ]
        return "\n\n".join(partes)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo armar el contexto de la operación %s", getattr(project, "slug", None))
        return ""


def origins_cited(answer: str) -> list[str]:
    """Los orígenes del contexto que la respuesta cita, en el orden fijo."""
    return [origen for origen in ORIGENES if origen in (answer or "")]
