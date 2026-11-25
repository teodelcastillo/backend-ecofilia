"""
El formulario de una operación: qué datos carga la institución al darla de
alta y cómo salen en la carátula del informe.

Hasta acá los campos eran los de CAF, escritos en el front (país, monto CAF,
tipo de operación, ejecutivo DATBC, ecosistemas estratégicos…). Ahora son
``operation_form`` en la configuración de cada institución:

- ``blocks``: el formulario, en bloques. El primero es la identidad: lleva el
  nombre de la operación y es el único con campos obligatorios.
- ``caratula``: el orden de las filas de la carátula (la tabla de datos de la
  Sección A). Puede nombrar claves sin campo, que salen sólo si tienen valor
  (datos viejos, como el número de operación).

Cada campo guarda su valor en ``context_notes[key]``. El back valida y guarda;
el armado del formulario y de la carátula es del front. Del back usan los
campos el contexto que reciben los asistentes y el chat: el rótulo de cada
dato y si va o no al análisis (``for_analysis``).

La configuración base trae un formulario genérico; el de CAF lo siembra
``user/0018_configuracion_completa_caf`` con lo que tenía el código.
"""
from __future__ import annotations

import copy
import re

FIELD_TYPES = ("text", "textarea", "number", "date", "url", "select", "multiselect", "etapa")
AI_MODES = {"fill": ("textarea",), "suggest": ("select",)}
# Lo que la IA sabe sacar del documento principal
# (``apps.project.services.ai_fill.EXTRACTABLE_FIELDS``): «fill» escribe el
# texto (con el botón «Generar»), «suggest» deja una opción marcada como
# sugerida para que la confirmen.
AI_KEYS = {"fill": ("objetivo", "componentes"), "suggest": ("tipo_operacion",)}
DEFAULTS = {"today": ("date",), "user_name": ("select", "text")}
# El texto libre de la opción «otros» de una selección múltiple va en esta clave.
OTHER_SUFFIX = "_otros"

FIELD_ATTRS = (
    "key", "label", "type", "required", "placeholder", "help", "options", "other_option", "none_option",
    "unit", "report_label", "wide", "in_report", "for_analysis", "ai", "default",
)
BLOCK_ATTRS = ("title", "description", "link", "stages", "only_edit", "collapsible", "report_title", "fields")

# La etapa es un campo más del formulario (para elegir dónde va), pero se
# guarda siempre en esta clave y se cambia con la acción de etapa.
STAGE_KEY = "estado"

# Lo que escribe el sistema en ``context_notes``, no el formulario: la mirada
# del ejecutivo, lo que hereda una etapa de otra, marcas de la IA y datos
# internos. Un campo con una de estas claves pisaría ese dato.
RESERVED_KEYS = frozenset(
    {
        "alineacion_paris", "alineacion_fundamentos", "alineacion_insumo", "consideraciones_adicionales",
        "criterio_ejecutivo", "ecofilia_ai_context", "ecofilia_output_titles", "elementos_clave_leidos",
        "enverdecimiento", "extra", "heredado_de", "historico", "motivos_diferencia", "name", "por_etapa",
        "precarga_de", "registro_trazabilidad", "tipo_operacion_sugerido", "year",
    }
)

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Para las instituciones sin formulario propio: los datos de cualquier
# operación de financiamiento, sin nada propio de CAF.
BASE_OPERATION_FORM: dict = {
    "blocks": [
        {
            "title": "Identidad",
            "fields": [
                {"key": "pais", "label": "País", "type": "text", "required": True, "placeholder": "Ej: Colombia"},
                {"key": STAGE_KEY, "label": "Etapa", "type": "etapa", "required": True},
            ],
        },
        {
            "title": "Datos opcionales",
            "collapsible": True,
            "fields": [
                {"key": "tipo_operacion", "label": "Tipo de operación", "type": "text"},
                {
                    "key": "monto",
                    "label": "Monto (millones de USD)",
                    "report_label": "Monto",
                    "type": "number",
                    "unit": "M USD",
                    "placeholder": "Ej: 150",
                },
                {"key": "fecha_informe", "label": "Fecha del informe", "type": "date", "default": "today", "for_analysis": False},
                {"key": "ubicacion_geografica", "label": "Ubicación geográfica"},
                {"key": "cliente", "label": "Cliente", "placeholder": "Entidad que recibe el financiamiento"},
                {"key": "ejecutor", "label": "Ejecutor", "placeholder": "Entidad que ejecuta la operación"},
            ],
        },
        {
            "title": "Contenido de la operación",
            "only_edit": True,
            "report_title": "Objetivo y componentes de la operación",
            "fields": [
                {
                    "key": "objetivo",
                    "label": "Objetivo",
                    "type": "textarea",
                    "ai": "fill",
                    "placeholder": "Describí el objetivo principal de la operación…",
                },
                {
                    "key": "componentes",
                    "label": "Componentes / Actividades",
                    "report_label": "Componentes y actividades",
                    "type": "textarea",
                    "ai": "fill",
                    "placeholder": "Listá los componentes o actividades principales de la operación…",
                },
            ],
        },
    ],
    "caratula": [],
}


def base_operation_form() -> dict:
    form = copy.deepcopy(BASE_OPERATION_FORM)
    return validate_operation_form(form, set(), [])


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _bool(value, path: str, errors: list[str], default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        errors.append(f"{path}: tiene que ser true o false.")
        return default
    return value


def _link(value, path: str, errors: list[str]) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append(f"{path}: tiene que ser un objeto con label y href, o null.")
        return None
    unknown = sorted(set(value) - {"label", "href"})
    if unknown:
        errors.append(f"{path}: claves desconocidas: {', '.join(unknown)}.")
    label, href = _text(value.get("label")), _text(value.get("href"))
    if not label:
        errors.append(f"{path}.label: no puede estar vacío.")
    if not (href.startswith("https://") or href.startswith("/")):
        errors.append(f"{path}.href: tiene que ser una URL https o una ruta del front (/...).")
    return {"label": label, "href": href}


def _field(item, path: str, first_block: bool, seen: set[str], errors: list[str]) -> dict | None:
    if not isinstance(item, dict):
        errors.append(f"{path}: tiene que ser un objeto.")
        return None
    unknown = sorted(set(item) - set(FIELD_ATTRS))
    if unknown:
        errors.append(f"{path}: claves desconocidas: {', '.join(unknown)}.")

    key = item.get("key")
    kind = item.get("type") or "text"
    if not isinstance(key, str) or not _KEY.match(key):
        errors.append(f"{path}.key: minúsculas, números y _, empezando con una letra.")
    elif key in RESERVED_KEYS:
        errors.append(f"{path}.key: «{key}» la usa el sistema; elegí otra.")
    elif key in seen:
        errors.append(f"{path}.key: «{key}» está repetida.")
    if isinstance(key, str):
        seen.add(key)

    if kind not in FIELD_TYPES:
        errors.append(f"{path}.type: tiene que ser uno de {', '.join(FIELD_TYPES)}.")
        kind = "text"
    if (kind == "etapa") != (key == STAGE_KEY):
        errors.append(f"{path}: la etapa es el campo «{STAGE_KEY}», de tipo etapa, y ningún otro.")

    label = _text(item.get("label"))
    if not label:
        errors.append(f"{path}.label: no puede estar vacío.")

    required = _bool(item.get("required"), f"{path}.required", errors, False)
    if required and not first_block:
        errors.append(f"{path}.required: sólo los campos del primer bloque (la identidad) pueden ser obligatorios.")

    options_raw = item.get("options")
    options: list[str] = []
    if kind in ("select", "multiselect"):
        if not isinstance(options_raw, list) or not options_raw:
            errors.append(f"{path}.options: tiene que ser una lista con al menos una opción.")
        else:
            for j, option in enumerate(options_raw):
                text = _text(option)
                if not text:
                    errors.append(f"{path}.options[{j}]: no puede estar vacía.")
                elif text in options:
                    errors.append(f"{path}.options[{j}]: «{text}» está repetida.")
                else:
                    options.append(text)
    elif options_raw not in (None, []):
        errors.append(f"{path}.options: sólo para los tipos select y multiselect.")

    other = _text(item.get("other_option")) or None
    if other and kind not in ("select", "multiselect"):
        errors.append(f"{path}.other_option: sólo para los tipos select y multiselect.")
    if other and other in options:
        errors.append(f"{path}.other_option: «{other}» ya es una de las opciones.")
    none = _text(item.get("none_option")) or None
    if none and kind != "multiselect":
        errors.append(f"{path}.none_option: sólo para el tipo multiselect.")
    if none and (none in options or none == other):
        errors.append(f"{path}.none_option: «{none}» ya es una de las opciones.")

    unit = _text(item.get("unit"))
    if unit and kind != "number":
        errors.append(f"{path}.unit: sólo para el tipo number.")

    ai = item.get("ai") or None
    if ai is not None and ai not in AI_MODES:
        errors.append(f"{path}.ai: tiene que ser fill, suggest o null.")
        ai = None
    if ai and kind not in AI_MODES[ai]:
        errors.append(f"{path}.ai: «{ai}» es para el tipo {' o '.join(AI_MODES[ai])}.")
    elif ai and key not in AI_KEYS[ai]:
        errors.append(f"{path}.ai: la IA sabe completar «{ai}» sólo en {', '.join(AI_KEYS[ai])}.")

    default = item.get("default") or None
    if default is not None and default not in DEFAULTS:
        errors.append(f"{path}.default: tiene que ser today, user_name o null.")
        default = None
    if default and kind not in DEFAULTS[default]:
        errors.append(f"{path}.default: «{default}» es para el tipo {' o '.join(DEFAULTS[default])}.")

    field = {
        "key": key,
        "label": label,
        "type": kind,
        "required": required,
        "placeholder": _text(item.get("placeholder")),
        "help": _text(item.get("help")),
        "options": options,
        "other_option": other,
        "none_option": none,
        "unit": unit,
        "report_label": _text(item.get("report_label")),
        "wide": _bool(item.get("wide"), f"{path}.wide", errors, False),
        "in_report": _bool(item.get("in_report"), f"{path}.in_report", errors, True),
        "for_analysis": _bool(item.get("for_analysis"), f"{path}.for_analysis", errors, True),
        "ai": ai,
        "default": default,
    }
    return field


def validate_operation_form(value, stage_slugs: set[str], errors: list[str]) -> dict:
    """El formulario normalizado. ``stage_slugs``: las etapas de la configuración."""
    path = "operation_form"
    if not isinstance(value, dict):
        errors.append(f"{path}: tiene que ser un objeto.")
        return {"blocks": [], "caratula": []}
    unknown = sorted(set(value) - {"blocks", "caratula"})
    if unknown:
        errors.append(f"{path}: claves desconocidas: {', '.join(unknown)}.")

    raw_blocks = value.get("blocks")
    if not isinstance(raw_blocks, list) or not raw_blocks:
        errors.append(f"{path}.blocks: tiene que haber al menos un bloque (la identidad).")
        raw_blocks = []
    seen: set[str] = set()
    blocks = []
    for i, raw in enumerate(raw_blocks):
        bpath = f"{path}.blocks[{i}]"
        if not isinstance(raw, dict):
            errors.append(f"{bpath}: tiene que ser un objeto.")
            continue
        unknown = sorted(set(raw) - set(BLOCK_ATTRS))
        if unknown:
            errors.append(f"{bpath}: claves desconocidas: {', '.join(unknown)}.")
        title = _text(raw.get("title"))
        if not title:
            errors.append(f"{bpath}.title: no puede estar vacío.")
        stages = raw.get("stages") or []
        if not isinstance(stages, list):
            errors.append(f"{bpath}.stages: tiene que ser una lista de etapas.")
            stages = []
        for j, stage in enumerate(stages):
            if stage not in stage_slugs:
                errors.append(f"{bpath}.stages[{j}]: «{stage}» no es una etapa de esta configuración.")
        only_edit = _bool(raw.get("only_edit"), f"{bpath}.only_edit", errors, False)
        if i == 0 and (stages or only_edit):
            errors.append(f"{bpath}: la identidad se muestra siempre; no lleva etapas ni «sólo al editar».")
        raw_fields = raw.get("fields")
        if not isinstance(raw_fields, list) or not raw_fields:
            errors.append(f"{bpath}.fields: tiene que haber al menos un campo.")
            raw_fields = []
        fields = [
            f
            for j, item in enumerate(raw_fields)
            if (f := _field(item, f"{bpath}.fields[{j}]", i == 0, seen, errors)) is not None
        ]
        blocks.append(
            {
                "title": title,
                "description": _text(raw.get("description")),
                "link": _link(raw.get("link"), f"{bpath}.link", errors),
                "stages": [s for s in dict.fromkeys(stages) if isinstance(s, str)],
                "only_edit": only_edit,
                "collapsible": _bool(raw.get("collapsible"), f"{bpath}.collapsible", errors, False),
                "report_title": _text(raw.get("report_title")),
                "fields": fields,
            }
        )
    stage_field_blocks = [
        i for i, b in enumerate(blocks) for f in b["fields"] if f["key"] == STAGE_KEY
    ]
    if stage_field_blocks and stage_field_blocks[0] != 0:
        errors.append(f"{path}: la etapa va en el primer bloque (la identidad).")
    for field in (f for b in blocks for f in b["fields"]):
        other_key = f"{field['key']}{OTHER_SUFFIX}"
        if field["type"] == "multiselect" and field["other_option"] and other_key in seen:
            errors.append(
                f"{path}: «{other_key}» guarda el texto de «{field['other_option']}» de «{field['key']}»; "
                "no puede ser otro campo."
            )

    raw_caratula = value.get("caratula") or []
    if not isinstance(raw_caratula, list):
        errors.append(f"{path}.caratula: tiene que ser una lista.")
        raw_caratula = []
    caratula, in_caratula = [], set()
    for i, entry in enumerate(raw_caratula):
        cpath = f"{path}.caratula[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{cpath}: tiene que ser un objeto con key (y label si no es un campo).")
            continue
        unknown = sorted(set(entry) - {"key", "label"})
        if unknown:
            errors.append(f"{cpath}: claves desconocidas: {', '.join(unknown)}.")
        key = entry.get("key")
        label = _text(entry.get("label"))
        if not isinstance(key, str) or not _KEY.match(key):
            errors.append(f"{cpath}.key: minúsculas, números y _, empezando con una letra.")
            continue
        if key in in_caratula:
            errors.append(f"{cpath}.key: «{key}» está repetida.")
            continue
        in_caratula.add(key)
        if key not in seen and not label:
            errors.append(f"{cpath}.label: «{key}» no es un campo del formulario; necesita un rótulo.")
        caratula.append({"key": key, **({"label": label} if label else {})})
    return {"blocks": blocks, "caratula": caratula}


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------

def fields(form: dict | None) -> list[dict]:
    """Todos los campos del formulario, en orden."""
    return [f for block in (form or {}).get("blocks") or [] for f in block.get("fields") or []]


def field_labels(form: dict | None) -> dict[str, str]:
    """El rótulo de cada campo, como lo escribe quien carga la operación."""
    return {f["key"]: f["label"] for f in fields(form) if f.get("key") and f.get("label")}


def keys_out_of_analysis(form: dict | None) -> set[str]:
    """Los campos que no van al contexto de los asistentes (datos de tramitación)."""
    return {f["key"] for f in fields(form) if f.get("for_analysis") is False}
