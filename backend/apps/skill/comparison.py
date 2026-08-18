"""
Comparar dos corridas: qué se mantuvo igual y qué se movió.

El reclamo original era que no se puede confiar en la salida porque dos corridas
con el mismo input no coinciden. Para poder afirmar o desmentir eso hace falta,
primero, poder demostrar que el input **fue** el mismo — y eso es una lista
cerrada de cosas: la definición, los documentos, los parámetros, el alcance, la
configuración de recuperación, el proveedor.

De ahí el orden de este módulo. Primero decide si las dos corridas son
**comparables**; sólo entonces tiene sentido mirar en qué difiere la salida. Dos
corridas que no son comparables pueden diferir por diez razones legítimas, y
tratarlas como evidencia de falta de determinismo es el error que hace perder
tardes enteras.

Del lado de la salida, lo que se compara no es principalmente la prosa: es la
**trazabilidad**. Que dos corridas redacten distinto la misma idea es esperable;
que citen páginas distintas para la misma afirmación, o que una determinación
tabular cambie de valor, no lo es. Por eso el diff reporta, paso por paso, el
conjunto de referencias y las filas de tabla antes que el parecido textual.

No importa modelos: trabaja por ``getattr`` sobre cualquier objeto con la forma
de una ejecución, así que se puede ejercitar sin base.
"""
from __future__ import annotations

import difflib

COMPARISON_SCHEMA = 1

# Comparar dos textos largos con difflib es cuadrático en el peor caso. Un paso
# de informe no llega a esto, pero un paso que se fue de las manos no debería
# colgar la comparación.
MAX_SIMILARITY_CHARS = 40_000

# Ejes del input que tienen que coincidir para que dos corridas sean
# comparables. El orden es el de "qué mirar primero" cuando no coinciden.
INPUT_AXES = (
    "definition",
    "documents",
    "input_values",
    "extra_instructions",
    "scope",
    "retrieval",
    "provider",
)


# ---------------------------------------------------------------------------
# Lectura de una ejecución
# ---------------------------------------------------------------------------

def _manifest(execution) -> dict:
    return dict((getattr(execution, "metadata", None) or {}).get("run_manifest") or {})


def _definition_snapshot(execution) -> dict:
    """La definición con la que corrió, si quedó registrada."""
    version = getattr(execution, "definition_version", None)
    manifest = _manifest(execution)
    if version is None:
        return {
            "version_number": None,
            "fingerprint": manifest.get("definition_fingerprint"),
            "definition": None,
            "recorded": False,
        }
    return {
        "version_number": version.version_number,
        "fingerprint": version.fingerprint,
        "definition": version.definition,
        "recorded": True,
        "changed_during_run": bool(manifest.get("definition_changed_during_run")),
    }


def _documents(execution) -> dict:
    """Los documentos del alcance, indexados por slug."""
    out = {}
    for row in getattr(execution, "document_snapshot", None) or []:
        if isinstance(row, dict) and row.get("slug"):
            out[row["slug"]] = row
    return out


def _steps(execution) -> list[dict]:
    structured = getattr(execution, "output_structured", None) or {}
    return [s for s in (structured.get("steps") or []) if isinstance(s, dict)]


def _references(step: dict) -> set[str]:
    """El conjunto de referencias verificables que el paso produjo.

    Se toma la referencia ya formateada (``"[ndc] p. 47"``) y no los offsets: lo
    que interesa comparar es lo que un auditor va a ir a buscar al documento.
    """
    out = set()
    for citation in step.get("citations") or []:
        if not isinstance(citation, dict):
            continue
        slug = citation.get("document_slug") or "?"
        out.add(f"[{slug}] {citation.get('reference') or 'sin página'}")
    return out


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

def _compare_definitions(a, b) -> dict:
    from apps.skill.definition import diff_definitions

    snap_a, snap_b = _definition_snapshot(a), _definition_snapshot(b)
    result = {"a": snap_a, "b": snap_b}

    if not (snap_a["recorded"] and snap_b["recorded"]):
        # Una corrida sin versión no se puede declarar igual ni distinta. Decir
        # "iguales" porque no hay con qué comparar sería el peor resultado
        # posible de una herramienta de auditoría.
        result["equal"] = None
        result["reason"] = (
            "una de las dos corridas no registró su definición (es anterior al "
            "versionado): no se puede afirmar que corrieron lo mismo."
        )
        return result

    result["equal"] = snap_a["fingerprint"] == snap_b["fingerprint"]
    if not result["equal"]:
        result["changes"] = diff_definitions(snap_a["definition"], snap_b["definition"])
    if snap_a.get("changed_during_run") or snap_b.get("changed_during_run"):
        result["warning"] = (
            "alguna de las corridas se reanudó después de que la definición "
            "cambiara: sus pasos no salieron todos de la misma definición."
        )
    return result


def _compare_documents(a, b) -> dict:
    docs_a, docs_b = _documents(a), _documents(b)
    only_a = sorted(set(docs_a) - set(docs_b))
    only_b = sorted(set(docs_b) - set(docs_a))
    reprocessed = []
    for slug in sorted(set(docs_a) & set(docs_b)):
        # `chunk_count` + `last_chunk_id` cambian cuando el documento se
        # reprocesa: mismo documento, otro texto indexado.
        huella_a = (docs_a[slug].get("chunk_count"), docs_a[slug].get("last_chunk_id"))
        huella_b = (docs_b[slug].get("chunk_count"), docs_b[slug].get("last_chunk_id"))
        if huella_a != huella_b:
            reprocessed.append({"slug": slug, "a": huella_a, "b": huella_b})
    return {
        "equal": not (only_a or only_b or reprocessed),
        "only_in_a": only_a,
        "only_in_b": only_b,
        "reprocessed": reprocessed,
        "count": [len(docs_a), len(docs_b)],
    }


def _compare_values(a, b) -> dict:
    return {"equal": False, "a": a, "b": b} if a != b else {"equal": True, "a": a}


def _compare_scope(a, b) -> dict:
    scope_a = _manifest(a).get("scope") or {}
    scope_b = _manifest(b).get("scope") or {}
    differences = {}
    for key in sorted(set(scope_a) | set(scope_b)):
        if scope_a.get(key) != scope_b.get(key):
            differences[key] = {"a": scope_a.get(key), "b": scope_b.get(key)}
    return {"equal": not differences, "changes": differences}


def _compare_retrieval(a, b) -> dict:
    ret_a = _manifest(a).get("retrieval") or {}
    ret_b = _manifest(b).get("retrieval") or {}
    differences = {}
    for key in sorted(set(ret_a) | set(ret_b)):
        if ret_a.get(key) != ret_b.get(key):
            differences[key] = {"a": ret_a.get(key), "b": ret_b.get(key)}
    return {"equal": not differences, "changes": differences}


def _compare_provider(a, b) -> dict:
    man_a, man_b = _manifest(a), _manifest(b)
    models_a = sorted(man_a.get("models_used") or [])
    models_b = sorted(man_b.get("models_used") or [])
    return {
        "equal": man_a.get("provider") == man_b.get("provider") and models_a == models_b,
        "provider": {"a": man_a.get("provider"), "b": man_b.get("provider")},
        "models_used": {"a": models_a, "b": models_b},
    }


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------

def _similarity(a: str, b: str) -> float:
    a, b = (a or "")[:MAX_SIMILARITY_CHARS], (b or "")[:MAX_SIMILARITY_CHARS]
    if not a and not b:
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def _compare_table(step_a: dict, step_b: dict) -> dict | None:
    table_a = step_a.get("table") or {}
    table_b = step_b.get("table") or {}
    if not table_a and not table_b:
        return None
    rows_a = table_a.get("rows") or []
    rows_b = table_b.get("rows") or []
    cells: list[dict] = []
    for index in range(max(len(rows_a), len(rows_b))):
        row_a = rows_a[index] if index < len(rows_a) else None
        row_b = rows_b[index] if index < len(rows_b) else None
        if row_a == row_b:
            continue
        if not isinstance(row_a, dict) or not isinstance(row_b, dict):
            cells.append({"row": index, "a": row_a, "b": row_b})
            continue
        for column in sorted(set(row_a) | set(row_b)):
            if row_a.get(column) != row_b.get(column):
                cells.append({
                    "row": index,
                    "column": column,
                    "a": row_a.get(column),
                    "b": row_b.get(column),
                })
    return {
        "equal": rows_a == rows_b,
        "rows": [len(rows_a), len(rows_b)],
        "cells_changed": cells,
    }


def _compare_step(step_a: dict, step_b: dict) -> dict:
    refs_a, refs_b = _references(step_a), _references(step_b)
    content_a = step_a.get("content") or ""
    content_b = step_b.get("content") or ""
    table = _compare_table(step_a, step_b)

    entry = {
        "title": step_a.get("title") or step_b.get("title"),
        "status": "identical",
        "output_mode": {"a": step_a.get("output_mode"), "b": step_b.get("output_mode")},
        "model": {"a": step_a.get("model"), "b": step_b.get("model")},
        "chars": [len(content_a), len(content_b)],
        "similarity": _similarity(content_a, content_b),
        "references": {
            "equal": refs_a == refs_b,
            "count": [len(refs_a), len(refs_b)],
            "only_in_a": sorted(refs_a - refs_b),
            "only_in_b": sorted(refs_b - refs_a),
        },
    }
    if table is not None:
        entry["table"] = table

    changed = (
        content_a != content_b
        or refs_a != refs_b
        or (table is not None and not table["equal"])
        or step_a.get("output_mode") != step_b.get("output_mode")
    )
    entry["status"] = "changed" if changed else "identical"
    return entry


def _compare_outputs(a, b) -> dict:
    steps_a, steps_b = _steps(a), _steps(b)
    entries: list[dict] = []
    for index in range(max(len(steps_a), len(steps_b))):
        if index >= len(steps_a):
            entries.append({
                "position": index + 1,
                "title": steps_b[index].get("title"),
                "status": "only_in_b",
            })
            continue
        if index >= len(steps_b):
            entries.append({
                "position": index + 1,
                "title": steps_a[index].get("title"),
                "status": "only_in_a",
            })
            continue
        entry = _compare_step(steps_a[index], steps_b[index])
        entry["position"] = index + 1
        entries.append(entry)

    identical = sum(1 for e in entries if e["status"] == "identical")
    return {
        "steps": entries,
        "steps_total": [len(steps_a), len(steps_b)],
        "steps_identical": identical,
        "steps_changed": len(entries) - identical,
        # El dato que se pidió medir: cuántos pasos citan exactamente las mismas
        # páginas. Es la reproducibilidad que le importa a un informe auditable
        # — más que si la redacción coincide palabra por palabra.
        "steps_with_same_references": sum(
            1 for e in entries if (e.get("references") or {}).get("equal")
        ),
    }


# ---------------------------------------------------------------------------
# API del módulo
# ---------------------------------------------------------------------------

_AXIS_LABELS = {
    "definition": "la definición del workflow",
    "documents": "el alcance documental",
    "input_values": "los parámetros de la corrida",
    "extra_instructions": "las instrucciones extra",
    "scope": "el alcance elegido al lanzar",
    "retrieval": "la configuración de recuperación",
    "provider": "el proveedor o los modelos usados",
}


def compare_executions(a, b) -> dict:
    """
    Comparar dos ejecuciones. ``a`` es la referencia, ``b`` la que se contrasta.
    """
    input_report = {
        "definition": _compare_definitions(a, b),
        "documents": _compare_documents(a, b),
        "input_values": _compare_values(
            dict(getattr(a, "input_values", None) or {}),
            dict(getattr(b, "input_values", None) or {}),
        ),
        "extra_instructions": _compare_values(
            (getattr(a, "extra_instructions", "") or "").strip(),
            (getattr(b, "extra_instructions", "") or "").strip(),
        ),
        "scope": _compare_scope(a, b),
        "retrieval": _compare_retrieval(a, b),
        "provider": _compare_provider(a, b),
    }

    differences = [
        _AXIS_LABELS[axis]
        for axis in INPUT_AXES
        if input_report[axis].get("equal") is False
    ]
    unknown = [
        _AXIS_LABELS[axis]
        for axis in INPUT_AXES
        if input_report[axis].get("equal") is None
    ]

    return {
        "schema": COMPARISON_SCHEMA,
        "executions": [
            {
                "id": getattr(x, "id", None),
                "status": getattr(x, "status", None),
                "created_at": (
                    getattr(x, "created_at", None).isoformat()
                    if getattr(x, "created_at", None)
                    else None
                ),
                "rerun_of": (getattr(x, "metadata", None) or {}).get("rerun_of"),
            }
            for x in (a, b)
        ],
        # `None` y no `False` cuando falta información: "no sabemos si el input
        # fue el mismo" es una respuesta distinta de "el input fue distinto", y
        # confundirlas es cómo se le atribuye al modelo una diferencia de input.
        "comparable": None if unknown and not differences else not differences,
        "input": input_report,
        "input_differences": differences,
        "input_unknown": unknown,
        "output": _compare_outputs(a, b),
    }
