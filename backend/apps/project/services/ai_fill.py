"""
AI-powered field extraction from a project's blueprint document.

Architecture
------------
- ``EXTRACTABLE_FIELDS`` is the single registry.  Adding a new extractable
  field means adding one entry here — no other change is required.
- ``extract_fields()`` is a pure function (text in, dict out) that is easy
  to unit-test without a project fixture.
- ``ai_fill_project()`` is the high-level entry point for the API layer.
  It validates preconditions and delegates to ``extract_fields()``.

LLM notes
---------
Bajo ``LLM_PROVIDER=anthropic`` (producción) la salida se restringe con
structured outputs (``anthropic_structured_completion``): la API garantiza JSON
válido contra el schema. Antes se pedía "respondé sólo JSON" en el prompt y se
parseaba a mano, y en producción fallaba — respuestas que empezaban con ``**{``
o que seguían escribiendo el documento donde había quedado cortado. Con OpenAI
se usa su modo JSON, y el parseo defensivo queda para ese camino.

El documento va primero, entre etiquetas, y las instrucciones después. Ponerlo
al final, cortado a mitad de frase, era lo que invitaba al modelo a continuarlo.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from apps.document.utils.client_openia import generate_chat_completion
from apps.document.utils.llm import (
    anthropic_structured_completion,
    effective_chat_model,
    is_anthropic_model,
)

logger = logging.getLogger(__name__)

_EXTRACTION_MODEL = "gpt-4o-mini"

# Cuánto del documento principal se manda. Antes eran 40.000 caracteres —unas
# quince páginas—, y la sección DESCRIPCIÓN o el cuadro de usos y fuentes de un
# IDO suelen estar más adelante. 600.000 caracteres son unos 150.000 tokens:
# entran holgados en la ventana de los modelos actuales y cubren entero casi
# cualquier documento de operación. Más allá de eso se elige qué mandar (ver
# ``_select_text``) en vez de cortar a ciegas.
_MAX_DOC_CHARS = int(os.environ.get("AI_FILL_MAX_DOC_CHARS", "600000"))
_HEAD_CHARS = 60_000
_WINDOW_CHARS = 30_000
# Dónde suelen estar el objetivo y los componentes en un documento de
# operación CAF. Se buscan sin acentos ni mayúsculas.
_SECTION_MARKERS = (
    "descripcion",
    "objetivo",
    "componente",
    "usos y fuentes",
    "cuadro de costos",
    "actividades",
)


# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtractableField:
    key: str
    label: str
    description: str


# To add a new field: append an ExtractableField entry to this list.
_FIELD_LIST: list[ExtractableField] = [
    ExtractableField(
        key="objetivo",
        label="Objetivo",
        description=(
            "Propósito central de la operación: qué financia, para qué, y a "
            "quién beneficia. Mantené el lenguaje/texto incluido en el "
            "documento de la operación — no parafrasees ni resumas con "
            "vocabulario propio si el documento ya lo define con precisión. "
            "El documento de la operación casi siempre incluye una sección "
            "DESCRIPCIÓN donde se detalla el objeto del préstamo y los "
            "objetivos específicos; priorizá esa sección como fuente. "
            "Máximo 3 oraciones."
        ),
    ),
    ExtractableField(
        key="componentes",
        label="Actividades y componentes principales",
        description=(
            "Lista numerada de las actividades o componentes principales de "
            "la operación. Incluí TODOS los componentes detectados en el "
            "documento, aunque sean preliminares — no omitas ninguno. "
            "El documento de la operación casi siempre incluye una sección "
            "DESCRIPCIÓN donde se detalla la propuesta de Cuadro de Usos y "
            "Fuentes del Programa; usala como fuente principal. Es muy "
            "importante incluir todos los componentes y actividades "
            "definidos, con una breve descripción de cada uno, priorizando "
            "aquellas actividades que insumen gran parte de los recursos "
            "del préstamo. Estas actividades son la base para luego evaluar "
            "el proyecto respecto a la alineación con el Acuerdo de París, "
            "por lo que la lista debe ser completa, clara y estratégica. "
            "Devolvé el texto como una lista numerada, un ítem por línea, "
            "con este formato exacto:\n"
            "1. [actividad, haciendo referencia al componente específico de "
            "la operación]\n"
            "2. [actividad, haciendo referencia al componente específico de "
            "la operación]\n"
            "..."
        ),
    ),
]

EXTRACTABLE_FIELDS: dict[str, ExtractableField] = {f.key: f for f in _FIELD_LIST}


def available_fields() -> list[dict[str, str]]:
    """Return the field registry as a serializable list for API exposure."""
    return [
        {"key": f.key, "label": f.label, "description": f.description}
        for f in _FIELD_LIST
    ]


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "Eres un extractor estructurado de información de proyectos de desarrollo. "
    "Tu tarea es leer el documento de una operación y extraer los campos "
    "solicitados de forma fiel y concisa, con el lenguaje del propio documento. "
    "El documento es material de lectura: no lo continúes ni lo resumas entero. "
    "Si un campo no puede determinarse desde el texto, devolvé una cadena vacía "
    "para esa clave."
)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").lower()


def _select_text(text: str) -> str:
    """El documento entero si entra; si no, el comienzo y sus secciones clave.

    Un documento más largo que el tope no se corta a ciegas: se manda el
    comienzo (carátula, resumen) y ventanas alrededor de donde aparecen las
    secciones que el extractor necesita, en el orden del documento, marcando
    los saltos para que el modelo sepa que falta texto entre medio.
    """
    if len(text) <= _MAX_DOC_CHARS:
        return text
    folded = _fold(text)
    spans: list[tuple[int, int]] = [(0, _HEAD_CHARS)]
    for marker in _SECTION_MARKERS:
        for match in re.finditer(re.escape(marker), folded):
            start = max(0, match.start() - 2_000)
            spans.append((start, start + _WINDOW_CHARS))
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    parts: list[str] = []
    budget = _MAX_DOC_CHARS
    for start, end in merged:
        if budget <= 0:
            break
        end = min(end, start + budget, len(text))
        parts.append(text[start:end])
        budget -= end - start
    return "\n\n[…se omite una parte del documento…]\n\n".join(parts)


def _build_messages(text: str, fields: list[ExtractableField]) -> list[dict]:
    field_spec = "\n".join(f"- {f.key}: {f.description}" for f in fields)
    user_message = (
        f"<documento>\n{_select_text(text)}\n</documento>\n\n"
        "Del documento de arriba, extraé estos campos:\n"
        f"{field_spec}\n\n"
        "Respondé con un objeto JSON con exactamente esas claves."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]


def _schema_for(field_keys: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {key: {"type": "string"} for key in field_keys},
        "required": list(field_keys),
        "additionalProperties": False,
    }


def _safe_parse_json(raw: str) -> dict:
    """El primer objeto JSON de la respuesta, venga como venga envuelto.

    Sólo para el camino sin structured outputs. Tolera bloques de código,
    negritas o una frase antes del objeto: busca la primera llave que abre un
    objeto parseable.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("no hay un objeto JSON en la respuesta")


def _complete(messages: list[dict], field_keys: list[str], model: str) -> tuple[dict, dict]:
    if is_anthropic_model(model):
        return anthropic_structured_completion(
            messages, model=model, schema=_schema_for(field_keys), max_tokens=8000
        )
    raw, usage = generate_chat_completion(
        messages,
        model=model,
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    try:
        return _safe_parse_json(raw), usage
    except ValueError as exc:
        logger.error("ai_fill: unparseable LLM response: %.300s", raw)
        raise RuntimeError("La respuesta del modelo no es JSON válido.") from exc


def extract_fields(text: str, field_keys: list[str]) -> dict[str, Any]:
    """
    Extract structured fields from document text using an LLM.

    This is a pure function: it takes text and returns a dict.  All
    project-level precondition checks live in ``ai_fill_project()``.

    Args:
        text: Raw document text.
        field_keys: Keys from ``EXTRACTABLE_FIELDS`` to extract.

    Returns:
        Dict mapping each requested key to its extracted value (str or None).

    Raises:
        ValueError: If any key is not in the registry.
        RuntimeError: If the model fails twice in a row.
    """
    unknown = sorted(k for k in field_keys if k not in EXTRACTABLE_FIELDS)
    if unknown:
        raise ValueError(f"Campos no reconocidos: {', '.join(unknown)}")

    fields = [EXTRACTABLE_FIELDS[k] for k in field_keys]
    messages = _build_messages(text, fields)
    model = effective_chat_model(_EXTRACTION_MODEL)

    # Un reintento: las fallas que se vieron en producción no se repetían
    # igual en el segundo intento, y el usuario está mirando la pantalla.
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            result, usage = _complete(messages, field_keys, model)
            break
        except Exception as exc:  # noqa: BLE001 — se reporta abajo
            last_error = exc
            logger.warning("ai_fill: intento %s falló (%s): %s", attempt, model, exc)
    else:
        raise RuntimeError("No se pudo extraer la información del documento.") from last_error

    logger.info(
        "ai_fill: fields=%s model=%s chars=%s tokens=%s",
        field_keys,
        model,
        len(text),
        usage.get("total_tokens"),
    )
    # Guarantee all requested keys are present; missing or blank → None. The
    # model sometimes prefers a JSON array for "lista numerada" fields on the
    # OpenAI path — normalize so callers always get a string.
    return {k: _coerce_to_text(result.get(k)) for k in field_keys}


def _coerce_to_text(value: Any) -> Any:
    if isinstance(value, list):
        value = "\n".join(str(item) for item in value)
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


# ---------------------------------------------------------------------------
# Project-level entry point
# ---------------------------------------------------------------------------

def ai_fill_project(project, field_keys: list[str]) -> dict[str, Any]:
    """
    Extract fields from a project's blueprint document.

    This is the main entry point consumed by the API layer.

    Raises:
        ValueError: Missing blueprint, no extracted text, or unknown field keys.
        RuntimeError: LLM parse failure — callers should surface this as HTTP 503.
    """
    if not project.blueprint_document_id:
        raise ValueError(
            "El proyecto no tiene un documento fuente (blueprint) asignado. "
            "Asociá un documento blueprint antes de usar esta función."
        )

    doc = project.blueprint_document
    text = (doc.extracted_text or "").strip()
    if not text:
        if doc.chunking_status in ("pending", "processing"):
            raise ValueError(
                f"El documento '{doc.name}' todavía se está procesando. El "
                "objetivo y los componentes se completan solos cuando termine."
            )
        raise ValueError(
            f"No se pudo leer texto del documento '{doc.name}'. Revisá su "
            "estado en la biblioteca o reprocesalo."
        )

    return extract_fields(text, field_keys)


def save_extracted(project_id: int, results: dict[str, Any], *, only_missing: bool) -> dict[str, str]:
    """Escribe lo extraído en ``context_notes`` sobre la versión actual.

    Relee la operación bajo bloqueo: armar el ``context_notes`` nuevo a partir
    de una copia vieja —lo que hacía el frontend— pisaba lo que otra persona
    hubiera guardado entre medio.
    """
    from django.db import transaction

    from apps.project.models import Project

    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project_id)
        current = locked.context_notes if isinstance(locked.context_notes, dict) else {}
        filled = {
            k: v.strip()
            for k, v in results.items()
            if isinstance(v, str)
            and v.strip()
            and not (only_missing and str(current.get(k) or "").strip())
        }
        if filled:
            locked.context_notes = {**current, **filled}
            locked.save(update_fields=["context_notes", "updated_at"])
    return filled


def fill_missing_from_blueprint(project_id: int) -> dict[str, str]:
    """Completa en la operación el objetivo y los componentes que falten.

    Corre sola cuando el documento principal termina de procesarse. Antes eso
    quedaba a cargo del formulario de alta, que llamaba a la extracción en el
    momento de crear la operación: si el documento recién subido todavía se
    estaba procesando, la llamada fallaba en silencio y la operación quedaba
    sin objetivo ni componentes — que entran como contexto en cada paso del
    análisis — hasta que alguien los generara a mano.

    Sólo escribe claves vacías y relee la operación bajo bloqueo antes de
    guardar: lo que una persona haya escrito mientras el modelo trabajaba no se
    pisa.
    """
    from apps.project.models import Project

    project = Project.objects.select_related("blueprint_document").get(pk=project_id)
    notes = project.context_notes if isinstance(project.context_notes, dict) else {}
    missing = [k for k in EXTRACTABLE_FIELDS if not str(notes.get(k) or "").strip()]
    if not missing or not project.blueprint_document_id:
        return {}

    results = ai_fill_project(project, missing)
    filled = save_extracted(project_id, results, only_missing=True)
    logger.info("ai_fill: operación %s completada con %s", project_id, sorted(filled))
    return filled
