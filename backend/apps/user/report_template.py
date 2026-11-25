"""
La plantilla del informe y el estilo del portal de una institución.

Son claves opcionales de la configuración (``report_template`` y tres claves de
``branding``): una configuración sin ellas sigue siendo válida y se ve igual
que antes. El back guarda la plantilla y la valida; no la interpreta. El
armado del informe y del Word es del front (``lib/plantilla-informe.ts``), que
valida con los mismos mensajes (``erroresDePlantilla``).

Contrato: ``backend/docs/contrato-instituciones.md``, «Plantilla del informe y
estilo del portal».
"""
from __future__ import annotations

import re

HEX = re.compile(r"^[0-9A-Fa-f]{6}$")
SECTION_KEY = re.compile(r"^([A-Z]{1,3}|\*)$")
LETTERHEAD_LOGO = re.compile(r"^(https://|/|data:image/(png|jpeg);base64,)")

BRANDING_OPTIONAL = ("portal_title", "primary_color", "portal_logo_url")
STYLE = (
    "body_font", "display_font", "text_color", "heading_color", "subheading_color",
    "accent_color", "table_header_color", "table_border_color",
)
SECTION = (
    "key", "heading", "intro", "source", "include_empty_fields", "include_objective", "conclusion",
    "groups", "inserts", "table_layout", "closing_heading", "closing",
)
TEMPLATE = ("title", "subtitle", "file_prefix", "assistant_name", "letterhead", "cover", "sections", "style")

HEX_MESSAGE = "un color hexadecimal de seis dígitos, sin «#»."


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def branding_optional(value: dict, errors: list[str]) -> dict:
    """Las claves opcionales de ``branding``. Las vacías no se guardan."""
    out = {}
    title = _text(value.get("portal_title")).strip()
    if title:
        out["portal_title"] = title
    color = _text(value.get("primary_color")).strip()
    if color:
        if HEX.match(color):
            out["primary_color"] = color.upper()
        else:
            errors.append(f"branding.primary_color: {HEX_MESSAGE}")
    logo = _text(value.get("portal_logo_url")).strip()
    if logo:
        if logo.startswith("/") or logo.startswith("https://"):
            out["portal_logo_url"] = logo
        else:
            errors.append("branding.portal_logo_url: tiene que ser una ruta del front (/...) o una URL https.")
    return out


def report_template(value, errors: list[str]):
    """``null`` (sin plantilla propia) o la plantilla normalizada."""
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append("report_template: tiene que ser un objeto o null.")
        return None
    p = "report_template"
    unknown = sorted(set(value) - set(TEMPLATE))
    if unknown:
        errors.append(f"{p}: claves desconocidas: {', '.join(unknown)}.")
    out: dict = {k: _text(value.get(k)) for k in ("title", "subtitle", "file_prefix", "assistant_name")}
    if not out["title"].strip():
        errors.append(f"{p}.title: no puede estar vacío.")

    letterhead = value.get("letterhead") if isinstance(value.get("letterhead"), dict) else {}
    unknown = sorted(set(letterhead) - {"logo_url", "logo_width", "logo_height", "lines"})
    if unknown:
        errors.append(f"{p}.letterhead: claves desconocidas: {', '.join(unknown)}.")
    lines = letterhead.get("lines") if isinstance(letterhead.get("lines"), list) else []
    out["letterhead"] = {
        "logo_url": _text(letterhead.get("logo_url")).strip(),
        "logo_width": letterhead.get("logo_width"),
        "logo_height": letterhead.get("logo_height"),
        "lines": [_text(x) for x in lines if _text(x).strip()],
    }
    if out["letterhead"]["logo_url"] and not LETTERHEAD_LOGO.match(out["letterhead"]["logo_url"]):
        errors.append(f"{p}.letterhead.logo_url: una URL https, una ruta del sitio o una imagen PNG/JPEG.")
    width, height = out["letterhead"]["logo_width"], out["letterhead"]["logo_height"]
    if not (_is_number(width) and 0 < width <= 600 and _is_number(height) and 0 < height <= 200):
        errors.append(f"{p}.letterhead: el logo mide hasta 600 × 200 píxeles.")

    cover = value.get("cover") if isinstance(value.get("cover"), dict) else {}
    unknown = sorted(set(cover) - {"intro", "structure_lead", "structure", "disclaimer"})
    if unknown:
        errors.append(f"{p}.cover: claves desconocidas: {', '.join(unknown)}.")
    structure = cover.get("structure") if isinstance(cover.get("structure"), list) else []
    out["cover"] = {
        "intro": _text(cover.get("intro")),
        "structure_lead": _text(cover.get("structure_lead")),
        "structure": [_text(x) for x in structure if _text(x).strip()],
        "disclaimer": _text(cover.get("disclaimer")),
    }

    style = value.get("style") if isinstance(value.get("style"), dict) else {}
    unknown = sorted(set(style) - set(STYLE))
    if unknown:
        errors.append(f"{p}.style: claves desconocidas: {', '.join(unknown)}.")
    out["style"] = {}
    for key in STYLE:
        item = _text(style.get(key)).strip()
        if key.endswith("_color") and not HEX.match(item):
            errors.append(f"{p}.style.{key}: {HEX_MESSAGE}")
        if key.endswith("_font") and not item:
            errors.append(f"{p}.style.{key}: no puede estar vacío.")
        out["style"][key] = item.upper() if key.endswith("_color") else item

    sections = value.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append(f"{p}.sections: tiene que haber al menos una sección.")
        sections = []
    out["sections"] = []
    keys: set[str] = set()
    for i, section in enumerate(sections):
        path = f"{p}.sections[{i}]"
        if not isinstance(section, dict):
            errors.append(f"{path}: tiene que ser un objeto.")
            continue
        unknown = sorted(set(section) - set(SECTION))
        if unknown:
            errors.append(f"{path}: claves desconocidas: {', '.join(unknown)}.")
        key = _text(section.get("key"))
        if not SECTION_KEY.match(key):
            errors.append(f"{path}.key: una a tres mayúsculas (A, B, IA) o «*».")
        elif key == "S":
            errors.append(f"{path}.key: «S» está reservada para los pasos internos del asistente.")
        elif key in keys:
            errors.append(f"{path}.key: «{key}» está repetida.")
        keys.add(key)
        source = section.get("source")
        if source not in ("operacion", "analisis"):
            errors.append(f"{path}.source: tiene que ser operacion o analisis.")
        if key == "*" and source != "analisis":
            errors.append(f"{path}: la sección «*» tiene que ser de análisis.")
        if not _text(section.get("heading")).strip():
            errors.append(f"{path}.heading: no puede estar vacío.")
        conclusion = section.get("conclusion")
        if conclusion not in (None, "criterios", "componentes"):
            errors.append(f"{path}.conclusion: criterios, componentes o null.")
        layout = section.get("table_layout")
        if layout not in (None, "mitades", "ultima_ancha"):
            errors.append(f"{path}.table_layout: mitades, ultima_ancha o null.")
        groups = section.get("groups") if isinstance(section.get("groups"), list) else []
        numbers: set[str] = set()
        for j, group in enumerate(groups):
            group = group if isinstance(group, dict) else {}
            number = _text(group.get("number"))
            if not number.isdigit():
                errors.append(f"{path}.groups[{j}].number: tiene que ser un número.")
            elif number in numbers:
                errors.append(f"{path}.groups[{j}].number: «{number}» está repetido.")
            numbers.add(number)
            if not _text(group.get("heading")).strip():
                errors.append(f"{path}.groups[{j}].heading: no puede estar vacío.")
        inserts = section.get("inserts") if isinstance(section.get("inserts"), list) else []
        for j, insert in enumerate(inserts):
            insert = insert if isinstance(insert, dict) else {}
            after = insert.get("after_step")
            if not (isinstance(after, int) and not isinstance(after, bool) and after >= 1):
                errors.append(f"{path}.inserts[{j}].after_step: tiene que ser un número de paso (1 o más).")
            if not _text(insert.get("heading")).strip():
                errors.append(f"{path}.inserts[{j}].heading: no puede estar vacío.")
        out["sections"].append(
            {
                "key": key,
                "heading": _text(section.get("heading")),
                "intro": _text(section.get("intro")),
                "source": source,
                "include_empty_fields": bool(section.get("include_empty_fields")),
                "include_objective": bool(section.get("include_objective", True)),
                "conclusion": conclusion,
                "groups": [
                    {"number": _text(g.get("number")), "heading": _text(g.get("heading"))}
                    for g in groups
                    if isinstance(g, dict)
                ],
                "inserts": [
                    {
                        "after_step": x.get("after_step"),
                        "heading": _text(x.get("heading")),
                        "instruction": _text(x.get("instruction")),
                    }
                    for x in inserts
                    if isinstance(x, dict)
                ],
                "table_layout": layout,
                "closing_heading": _text(section.get("closing_heading")),
                "closing": _text(section.get("closing")),
            }
        )
    if sum(1 for s in out["sections"] if s["source"] == "operacion") > 1:
        errors.append(f"{p}.sections: puede haber una sola sección con los datos de la operación.")
    return out
