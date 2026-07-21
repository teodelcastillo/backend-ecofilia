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
``generate_chat_completion()`` accepts ``response_format={"type":"json_object"}``
which enables OpenAI's guaranteed-JSON mode.  When the deployment routes to an
Anthropic model (``LLM_PROVIDER=anthropic``), the parameter is silently dropped
by the client layer.  To keep the output parseable in both cases:
  1. The system prompt instructs the model to respond with raw JSON only.
  2. ``_safe_parse_json()`` strips markdown code fences before parsing.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from apps.document.utils.client_openia import generate_chat_completion
from apps.document.utils.llm import effective_chat_model

logger = logging.getLogger(__name__)

_EXTRACTION_MODEL = "gpt-4o-mini"
_MAX_DOC_CHARS = 40_000


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
    "Tu tarea es leer el texto de un documento y extraer los campos solicitados "
    "de forma fiel y concisa. "
    "Responde ÚNICAMENTE con un objeto JSON válido que contenga exactamente "
    "las claves indicadas. "
    "Si un campo no puede determinarse con certeza desde el texto, devuelve "
    "null para esa clave. "
    "No incluyas texto, comentarios ni bloques de código fuera del JSON."
)


def _safe_parse_json(raw: str) -> dict:
    """Parse JSON from LLM output, stripping markdown code fences if present."""
    text = raw.strip()
    # Strip ```json ... ``` or ``` ... ``` wrappers
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return json.loads(text.strip())


def extract_fields(text: str, field_keys: list[str]) -> dict[str, Any]:
    """
    Extract structured fields from document text using an LLM.

    This is a pure function: it takes text and returns a dict.  All
    project-level precondition checks live in ``ai_fill_project()``.

    Args:
        text: Raw document text (will be truncated to ``_MAX_DOC_CHARS``).
        field_keys: Keys from ``EXTRACTABLE_FIELDS`` to extract.

    Returns:
        Dict mapping each requested key to its extracted value (str or None).

    Raises:
        ValueError: If any key is not in the registry.
        RuntimeError: If the LLM response cannot be parsed as JSON.
    """
    unknown = sorted(k for k in field_keys if k not in EXTRACTABLE_FIELDS)
    if unknown:
        raise ValueError(f"Campos no reconocidos: {', '.join(unknown)}")

    fields = [EXTRACTABLE_FIELDS[k] for k in field_keys]
    field_spec = "\n".join(f'  "{f.key}": {f.description}' for f in fields)
    keys_json = json.dumps(field_keys, ensure_ascii=False)

    user_message = (
        f"Extrae los siguientes campos del documento y devuelve un JSON "
        f"con exactamente estas claves: {keys_json}\n\n"
        f"Descripción de cada campo:\n{field_spec}\n\n"
        f"DOCUMENTO:\n{text[:_MAX_DOC_CHARS]}"
    )

    model = effective_chat_model(_EXTRACTION_MODEL)
    raw, usage = generate_chat_completion(
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        model=model,
        temperature=0.0,
        response_format={"type": "json_object"},
    )

    logger.info(
        "ai_fill: fields=%s model=%s tokens=%s",
        field_keys,
        model,
        usage.get("total_tokens"),
    )

    try:
        result = _safe_parse_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.error("ai_fill: unparseable LLM response: %.300s", raw)
        raise RuntimeError("La respuesta del modelo no es JSON válido.") from exc

    # Guarantee all requested keys are present; fill missing ones with None.
    return {k: result.get(k) for k in field_keys}


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
        raise ValueError(
            f"El documento '{doc.name}' no tiene texto extraído aún. "
            "Esperá a que termine el procesamiento del documento e intentá de nuevo."
        )

    return extract_fields(text, field_keys)
