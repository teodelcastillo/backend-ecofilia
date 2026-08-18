"""
Los parámetros declarados de un workflow, resueltos antes de correr.

Un parámetro tipado es lo que permite que el template mute sin tocar prompts: el
autor declara ``{{marco}}`` con sus valores posibles y el paso lo referencia. Eso
ya existía, pero sin nada que lo hiciera cumplir — ``input_values`` era un dict
libre. De ahí salían dos fallas silenciosas:

* un parámetro declarado con default que el llamador omitía dejaba el token
  literal ``{{marco}}`` dentro del prompt, y el modelo lo leía como texto;
* un valor fuera de las opciones declaradas entraba igual, así que el
  vocabulario cerrado no cerraba nada.

Las dos rompen la reproducibilidad de la peor forma: la corrida termina "bien" y la
diferencia con la corrida anterior no aparece en ningún lado.

Se valida al lanzar, no durante la corrida, porque acá sí hay alguien esperando
la respuesta: un parámetro mal puesto se arregla en el momento. Es lo contrario
del contrato tabular, que se descubre a mitad de camino.

Sin dependencia de modelos: trabaja por ``getattr``, igual que el resto del
motor nuevo.
"""
from __future__ import annotations

from datetime import date

MISSING_REQUIRED = "missing_required"
INVALID_NUMBER = "invalid_number"
INVALID_DATE = "invalid_date"
INVALID_ENUM = "invalid_enum"
INVALID_DEFAULT = "invalid_default"
UNKNOWN_PARAMETER = "unknown_parameter"

_TRUE = {"true", "1", "yes", "si", "sí", "on"}
_FALSE = {"false", "0", "no", "off", ""}


def _coerce(value, *, param_type: str, options) -> tuple[object, str | None]:
    """Un valor al tipo declarado. Devuelve ``(valor, problema)``."""
    if param_type == "number":
        try:
            text = str(value).strip().replace(",", ".")
            number = float(text)
        except (TypeError, ValueError):
            return None, INVALID_NUMBER
        return (int(number) if number.is_integer() else number), None

    if param_type == "boolean":
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in _TRUE:
            return True, None
        if text in _FALSE:
            return False, None
        return None, INVALID_ENUM

    if param_type == "date":
        try:
            return date.fromisoformat(str(value).strip()).isoformat(), None
        except (TypeError, ValueError):
            return None, INVALID_DATE

    if param_type == "enum":
        text = str(value).strip()
        allowed = [str(o) for o in (options or [])]
        if not allowed:
            # Un enum sin opciones declaradas no tiene vocabulario que hacer
            # cumplir. Es un error de definición, no del llamador: se deja pasar
            # el valor tal cual en vez de rechazar cualquier cosa.
            return text, None
        for option in allowed:
            # Una diferencia de capitalización es formato, no una violación —
            # misma política que las celdas de tabla.
            if option.strip().lower() == text.lower():
                return option, None
        return None, INVALID_ENUM

    return "" if value is None else str(value), None


def _issue(parameter, problem: str, received) -> dict:
    return {
        "key": getattr(parameter, "key", ""),
        "label": getattr(parameter, "label", "") or getattr(parameter, "key", ""),
        "problem": problem,
        "received": received,
        "allowed_values": [str(o) for o in (getattr(parameter, "options", None) or [])],
        "param_type": getattr(parameter, "param_type", "text"),
    }


def resolve_input_values(parameters, values: dict | None) -> tuple[dict, list[dict]]:
    """
    Los valores efectivos de la corrida, y los problemas encontrados.

    Aplica defaults, convierte al tipo declarado y verifica los vocabularios.
    Las claves que no corresponden a ningún parámetro declarado se conservan y se
    reportan como ``unknown_parameter``: casi siempre son un typo que dejaría un
    ``{{token}}`` sin resolver, pero rechazarlas rompería a cualquier llamador
    que hoy manda campos de más, así que la decisión de qué hacer con ellas queda
    en quien llama.
    """
    values = dict(values or {})
    declared = list(parameters or [])
    resolved: dict = {}
    issues: list[dict] = []

    for parameter in declared:
        key = getattr(parameter, "key", "")
        if not key:
            continue
        param_type = getattr(parameter, "param_type", "text") or "text"
        options = getattr(parameter, "options", None)
        required = bool(getattr(parameter, "required", False))
        raw = values.pop(key, None)
        supplied = raw not in (None, "")

        if not supplied:
            default = getattr(parameter, "default_value", "") or ""
            if default:
                value, problem = _coerce(default, param_type=param_type, options=options)
                if problem:
                    # El default es parte de la definición: si no cumple su
                    # propio tipo, el problema no es de esta corrida.
                    issues.append(_issue(parameter, INVALID_DEFAULT, default))
                    continue
                resolved[key] = value
                continue
            if required:
                issues.append(_issue(parameter, MISSING_REQUIRED, raw))
            continue

        value, problem = _coerce(raw, param_type=param_type, options=options)
        if problem:
            issues.append(_issue(parameter, problem, raw))
            continue
        resolved[key] = value

    for leftover_key, leftover in values.items():
        resolved[leftover_key] = leftover
        issues.append({
            "key": leftover_key,
            "label": leftover_key,
            "problem": UNKNOWN_PARAMETER,
            "received": leftover,
            "allowed_values": [],
            "param_type": "",
        })

    return resolved, issues


def blocking_issues(issues: list[dict]) -> list[dict]:
    """Los que impiden lanzar. Una clave desconocida no lo hace."""
    return [i for i in issues if i["problem"] != UNKNOWN_PARAMETER]


def describe_issue(issue: dict) -> str:
    label = issue.get("label") or issue.get("key")
    problem = issue.get("problem")
    if problem == MISSING_REQUIRED:
        return f"Falta el parámetro obligatorio «{label}»."
    if problem == INVALID_NUMBER:
        return f"«{label}» espera un número y recibió {issue.get('received')!r}."
    if problem == INVALID_DATE:
        return f"«{label}» espera una fecha ISO (AAAA-MM-DD) y recibió {issue.get('received')!r}."
    if problem == INVALID_ENUM:
        allowed = ", ".join(issue.get("allowed_values") or []) or "sí / no"
        return (
            f"«{label}» recibió {issue.get('received')!r}, que no está entre los "
            f"valores declarados ({allowed})."
        )
    if problem == INVALID_DEFAULT:
        return (
            f"El valor por defecto de «{label}» ({issue.get('received')!r}) no cumple "
            f"su propio tipo declarado: hay que corregir la definición del workflow."
        )
    if problem == UNKNOWN_PARAMETER:
        return f"«{label}» no es un parámetro declarado por este workflow."
    return f"«{label}»: {problem}"
