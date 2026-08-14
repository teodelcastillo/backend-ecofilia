"""
Query analysis utilities for the RAG pipeline.

Goals:
- Classify the user's question (factual / comparative / panorama / numeric).
- Optionally decompose broad questions into sub-questions to widen retrieval.
- Extract simple entities/keywords useful for lexical retrieval.

Heuristics first (cheap, deterministic) and optional LLM expansion behind an env
flag (RAG_QUERY_EXPANSION_ENABLED) to avoid extra cost during dev/tests.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List

logger = logging.getLogger(__name__)


QUERY_TYPE_FACTUAL = "factual"
QUERY_TYPE_NUMERIC = "numeric"
QUERY_TYPE_EXTRACTION_PER_ENTITY = "extract_per_entity"
QUERY_TYPE_COMPARATIVE = "comparative"
QUERY_TYPE_PANORAMA = "panorama"
QUERY_TYPE_EXTRACTION = QUERY_TYPE_NUMERIC

# Locality — where the answer lives (drives retrieve-vs-read depth).
LOCALITY_LOCALIZED = "localized"      # answer sits in one passage / document
LOCALITY_DISTRIBUTED = "distributed"  # answer spread across docs / sections

# Operation — what the task does with the evidence.
OPERATION_LOOKUP = "lookup"
OPERATION_EXTRACT = "extract"
OPERATION_COMPARE = "compare"
OPERATION_SYNTHESIZE = "synthesize"

COVERAGE_MODE_FOCUSED = "focused"
COVERAGE_MODE_BALANCED = "balanced"
COVERAGE_MODE_ALL = "all"

RESPONSE_MODE_PUNTUAL = "puntual"
RESPONSE_MODE_PANORAMA = "panorama"
RESPONSE_MODE_COMPARACION = "comparacion"
RESPONSE_MODE_EXTRACCION = "extraccion"
RESPONSE_MODE_TABLA = "tabla"

_PANORAMA_PATTERNS = (
    r"\b(resumen|panorama|vision|visión|overview|síntesis|sintesis)\b",
    r"\b(general|global|integral|completo|completa)\b",
    r"\b(todo|toda|todos|todas|cada (uno|una))\b",
    r"\b(base documental|documentacion|documentación|biblioteca|repositorio|repository)\b",
    r"\b(rasgos generales|de qu[eé] trata|en t[eé]rminos generales)\b",
    r"\b(overall|high[- ]level|across|across all)\b",
    # Listing / enumeration queries → treat as PANORAMA (need wide retrieval)
    r"\b(listados?|list[ao]|listame|listarme|listar|enumera[mr]?|enumeraci[oó]n)\b",
    r"\b(cu[aá]les son|cu[aá]les (son |fueron |han sido )?los|cu[aá]les (son |fueron |han sido )?las)\b",
    r"\b(qu[eé] pa[ií]ses|pa[ií]ses que|pa[ií]ses de (la |los |las )?)\b",
)

_ALL_COVERAGE_PATTERNS = (
    r"\b(cada documento|cada uno|cada una|uno por documento|una por documento)\b",
    r"\b(todos los documentos|todas las fuentes|documentos seleccionados)\b",
    r"\b(repositorio|repository|base documental|biblioteca|documentaci[oó]n)\b",
    r"\b(rasgos generales|panorama general|visi[oó]n general|overview|overall)\b",
)

_COMPARATIVE_PATTERNS = (
    r"\b(compar[aá]r?|compara|comparativo|comparativa|comparativos|comparativas)\b",
    r"\b(diferencias?|similitudes?|versus|vs\.?|frente a)\b",
    r"\b(entre [^.,]+? y )",
    r"\b(cu[áa]l (es )?(mejor|peor)|ranking)\b",
)

_NUMERIC_PATTERNS = (
    r"\b(cu[áa]nt[oa]s?|how many|how much|porcentaje|%|monto|valor|t[oó]nelad[ao]s?|kg|mw|gw|kwh)\b",
    r"\b(emisiones|consumo|huella|capex|opex|ebitda|ingresos?|revenue|margen|roe|roi)\b",
)

# Per-entity extraction: "X de/para cada documento/uno". Detected only when a
# per-each marker co-occurs with an extraction signal — this is the "extraé la
# meta de cada NDC" shape that needs a per-document map, not a single shared pass.
_PER_ENTITY_PATTERNS = (
    r"\bde cada\b",
    r"\bpara cada\b",
    r"\bpor cada\b",
    r"\bcada (uno|una|documento|fuente|archivo|ndc|pa[ií]s|informe|reporte)\b",
    r"\b(uno|una) por (documento|cada|archivo|fuente)\b",
    r"\bde (todas|todos) (las|los)\b",
    r"\bfor each\b",
    r"\beach (of|document|one|file)\b",
)
_EXTRACTION_VERB_PATTERNS = (
    r"\b(extrae|extra[eé]|lista|list[aá]|listame|enumera|enumer[aá]|"
    r"identifica|identific[aá]|indica|indic[aá]|menciona|mencion[aá]|"
    r"obten|obt[eé]n|saca|sac[aá]|dame|resume|resum[ií])\b",
    r"\b(cu[aá]l(es)? (es|son|fue|fueron|ser[ií]a))\b",
    r"\b(meta|metas|objetivo|objetivos|valor|valores|cifra|cifras|monto|"
    r"porcentaje|dato|datos|indicador|indicadores)\b",
)


CLASSIFIER_SOURCE_REGEX = "regex"
CLASSIFIER_SOURCE_LLM = "llm_router"
CLASSIFIER_SOURCE_OVERRIDE = "override"

CLASSIFIER_CONFIDENCE_HIGH = "high"
CLASSIFIER_CONFIDENCE_MEDIUM = "medium"
CLASSIFIER_CONFIDENCE_LOW = "low"


@dataclass
class QueryAnalysis:
    """Structured representation of the user's question for retrieval."""

    raw_text: str
    normalized: str
    query_type: str = QUERY_TYPE_FACTUAL
    is_general: bool = False
    coverage_mode: str = COVERAGE_MODE_FOCUSED
    locality: str = LOCALITY_LOCALIZED
    operation: str = OPERATION_LOOKUP
    keywords: List[str] = field(default_factory=list)
    numeric_tokens: List[str] = field(default_factory=list)
    sub_queries: List[str] = field(default_factory=list)
    # Telemetry: how the classification was reached. Not exposed in
    # ``to_dict`` to avoid leaking implementation details to legacy clients;
    # the chat views re-attach these to ``rag_diagnostics`` explicitly.
    classifier_source: str = CLASSIFIER_SOURCE_REGEX
    classifier_confidence: str = CLASSIFIER_CONFIDENCE_HIGH

    def to_dict(self) -> dict:
        return {
            "query_type": self.query_type,
            "is_general": self.is_general,
            "coverage_mode": self.coverage_mode,
            "locality": self.locality,
            "operation": self.operation,
            "keywords": self.keywords,
            "numeric_tokens": self.numeric_tokens,
            "sub_queries": self.sub_queries,
        }


# Locality / operation derived from the coarse query type.
_LOCALITY_OPERATION = {
    QUERY_TYPE_FACTUAL: (LOCALITY_LOCALIZED, OPERATION_LOOKUP),
    QUERY_TYPE_NUMERIC: (LOCALITY_LOCALIZED, OPERATION_EXTRACT),
    QUERY_TYPE_EXTRACTION_PER_ENTITY: (LOCALITY_DISTRIBUTED, OPERATION_EXTRACT),
    QUERY_TYPE_COMPARATIVE: (LOCALITY_DISTRIBUTED, OPERATION_COMPARE),
    QUERY_TYPE_PANORAMA: (LOCALITY_DISTRIBUTED, OPERATION_SYNTHESIZE),
}


def _locality_operation(query_type: str) -> tuple[str, str]:
    return _LOCALITY_OPERATION.get(query_type, (LOCALITY_LOCALIZED, OPERATION_LOOKUP))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _extract_keywords(text: str, max_terms: int = 12) -> List[str]:
    """Cheap keyword extraction: words >= 4 chars, drop trivial stopwords."""
    if not text:
        return []
    stop = {
        # ES
        "para", "como", "cual", "cuál", "cuáles", "cuales", "donde", "dónde",
        "cuando", "cuándo", "porque", "según", "segun", "entre", "sobre", "todo",
        "todos", "todas", "toda", "este", "esta", "esto", "ese", "esa", "eso",
        "tener", "tiene", "hacer", "decir", "puedes", "puede", "pueden", "favor",
        "informacion", "información", "lista", "listame", "listar", "necesito",
        "quisiera",
        # EN
        "what", "which", "where", "when", "with", "from", "into", "about",
        "please", "could", "would", "should", "have", "their", "there", "these",
        "those", "list", "show",
    }
    tokens = [t for t in re.findall(r"[a-záéíóúñü0-9]+", text.lower()) if len(t) >= 4]
    seen: set[str] = set()
    out: List[str] = []
    for t in tokens:
        if t in stop or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_terms:
            break
    return out


def _extract_numeric_tokens(text: str) -> List[str]:
    return re.findall(r"\d+(?:[\.,]\d+)?", text or "")


def _classifier_confidence(
    *,
    is_panorama: bool,
    all_coverage_hits: int,
    is_comparative: bool,
    is_numeric: bool,
    long_question: bool,
    word_count: int,
) -> str:
    """
    Heuristic confidence in the regex classifier's decision.

    "high":    one strong signal (clearly comparative, clearly panorama,
               or clearly numeric without mixed cues).
    "medium":  conflicting signals (e.g. comparative + numeric), or only a
               weak fallback (default factual on a long question with no
               other signals).
    "low":     no clear classification signal and an ambiguous length.
               These are the queries an LLM router should disambiguate.
    """
    flags = sum(1 for f in (is_panorama, is_comparative, is_numeric) if f)
    # Two strong signals contradict each other -> the LLM is the tiebreaker.
    if flags >= 2:
        return CLASSIFIER_CONFIDENCE_MEDIUM
    # Comparative is very unambiguous when it fires alone.
    if is_comparative and flags == 1:
        return CLASSIFIER_CONFIDENCE_HIGH
    if is_panorama and flags == 1:
        return CLASSIFIER_CONFIDENCE_HIGH
    # Numeric on its own with explicit unit/keyword cues is usually right;
    # mere presence of a digit in a longer prose question is weaker.
    if is_numeric and flags == 1 and all_coverage_hits == 0:
        return CLASSIFIER_CONFIDENCE_HIGH
    # No signals at all: short factual lookups are confident; longer prose
    # falls into the ambiguous "factual by default" bucket which is the
    # main miss case the regex classifier has today.
    if flags == 0:
        if word_count <= 8:
            return CLASSIFIER_CONFIDENCE_HIGH
        if long_question:
            # Long question with zero topical signals — LLM should look at it.
            return CLASSIFIER_CONFIDENCE_LOW
        return CLASSIFIER_CONFIDENCE_MEDIUM
    return CLASSIFIER_CONFIDENCE_MEDIUM


def classify_query(text: str) -> QueryAnalysis:
    """
    Classify a user query into a coarse RAG-relevant type using regex
    heuristics. Always returns a QueryAnalysis; never raises on bad input.

    The result includes ``classifier_source`` and ``classifier_confidence``
    so the hybrid router and downstream diagnostics can reason about it.
    """
    norm = _normalize(text)
    analysis = QueryAnalysis(raw_text=text or "", normalized=norm)
    if not norm:
        analysis.classifier_confidence = CLASSIFIER_CONFIDENCE_HIGH
        return analysis

    analysis.keywords = _extract_keywords(norm)
    analysis.numeric_tokens = _extract_numeric_tokens(norm)

    is_panorama = any(re.search(p, norm) for p in _PANORAMA_PATTERNS)
    all_coverage_hits = sum(1 for p in _ALL_COVERAGE_PATTERNS if re.search(p, norm))
    is_comparative = any(re.search(p, norm) for p in _COMPARATIVE_PATTERNS)
    is_numeric = any(re.search(p, norm) for p in _NUMERIC_PATTERNS) or bool(
        analysis.numeric_tokens
    )
    # Per-entity extraction needs BOTH a per-each marker and an extraction signal
    # ("extraé la meta de cada NDC"): a per-document map, not one shared pass.
    is_per_entity = any(
        re.search(p, norm) for p in _PER_ENTITY_PATTERNS
    ) and any(re.search(p, norm) for p in _EXTRACTION_VERB_PATTERNS)

    word_count = len(norm.split())
    long_question = word_count >= 18

    if is_per_entity:
        analysis.query_type = QUERY_TYPE_EXTRACTION_PER_ENTITY
    elif is_comparative:
        analysis.query_type = QUERY_TYPE_COMPARATIVE
    elif is_panorama or long_question:
        analysis.query_type = QUERY_TYPE_PANORAMA
    elif is_numeric:
        analysis.query_type = QUERY_TYPE_NUMERIC
    else:
        analysis.query_type = QUERY_TYPE_FACTUAL

    analysis.is_general = analysis.query_type in {
        QUERY_TYPE_PANORAMA,
        QUERY_TYPE_COMPARATIVE,
        QUERY_TYPE_EXTRACTION_PER_ENTITY,
    } or long_question

    # Per-entity extraction must cover EVERY document (one answer per entity),
    # so it forces ALL coverage even with a single per-each marker.
    if is_per_entity or all_coverage_hits >= 2:
        analysis.coverage_mode = COVERAGE_MODE_ALL
    elif analysis.query_type in {QUERY_TYPE_PANORAMA, QUERY_TYPE_COMPARATIVE}:
        analysis.coverage_mode = COVERAGE_MODE_BALANCED
    else:
        analysis.coverage_mode = COVERAGE_MODE_FOCUSED

    analysis.locality, analysis.operation = _locality_operation(analysis.query_type)
    analysis.sub_queries = _heuristic_sub_queries(analysis)
    analysis.classifier_source = CLASSIFIER_SOURCE_REGEX
    analysis.classifier_confidence = _classifier_confidence(
        is_panorama=is_panorama,
        all_coverage_hits=all_coverage_hits,
        is_comparative=is_comparative,
        is_numeric=is_numeric,
        long_question=long_question,
        word_count=word_count,
    )
    # A co-occurring per-each marker + extraction verb is an unambiguous signal.
    if is_per_entity:
        analysis.classifier_confidence = CLASSIFIER_CONFIDENCE_HIGH
    return analysis


_LLM_ROUTER_SYSTEM_PROMPT = (
    "Eres un router de un sistema RAG. Dada la pregunta del usuario, "
    "decide cómo se debe orientar la recuperación. "
    "Responde EXCLUSIVAMENTE con un JSON con la forma:\n"
    "{\n"
    '  "query_type": "factual" | "numeric" | "extract_per_entity" | "comparative" | "panorama",\n'
    '  "coverage_mode": "focused" | "balanced" | "all",\n'
    '  "is_general": true | false,\n'
    '  "confidence": "high" | "medium" | "low"\n'
    "}\n\n"
    "Lineamientos:\n"
    "- factual: pregunta específica con respuesta puntual.\n"
    "- numeric: pide cifras, métricas, montos, porcentajes o cantidades.\n"
    "- extract_per_entity: pide extraer un dato/campo para CADA documento o "
    "entidad (p.ej. 'la meta de cada NDC'). Requiere coverage_mode 'all'.\n"
    "- comparative: contrasta dos o más cosas, o pide ranking.\n"
    "- panorama: pide visión general, síntesis o resumen amplio.\n"
    "- coverage_mode all: cuando se necesita cubrir cada documento; "
    "balanced para preguntas amplias; focused para preguntas puntuales.\n"
    "- is_general: true para preguntas amplias que se beneficien de varias "
    "sub-consultas; false si es específica.\n"
    "No incluyas texto fuera del JSON."
)


def is_llm_router_enabled() -> bool:
    """
    ¿El router LLM está activo?

    Público a propósito: el manifiesto de corrida
    (``apps.skill.services``) necesita registrar el valor efectivo, y leerlo de
    acá evita que un default reimplementado se desincronice y haga que el
    manifiesto mienta sobre la corrida que dice describir.
    """
    return os.environ.get("RAG_LLM_ROUTER_ENABLED", "1").lower() in (
        "1", "true", "yes", "on",
    )


# Alias interno histórico; el resto del módulo lo usa con este nombre.
_llm_router_enabled = is_llm_router_enabled


def is_query_expansion_enabled() -> bool:
    """¿La expansión de sub-consultas por LLM está activa? Ver nota en ``is_llm_router_enabled``."""
    return os.environ.get("RAG_QUERY_EXPANSION_ENABLED", "0").lower() in (
        "1", "true", "yes", "on",
    )


def classify_query_llm(text: str) -> QueryAnalysis | None:
    """
    Classify the query with a lightweight LLM router. Returns ``None`` if
    the LLM is disabled, unavailable, or the response cannot be parsed.

    The returned ``QueryAnalysis`` preserves the regex-derived ``keywords``,
    ``numeric_tokens`` and ``sub_queries`` (callers should pass through a
    regex-classified ``QueryAnalysis`` via ``classify_query_hybrid``); when
    invoked standalone it returns an analysis with empty keyword fields.
    """
    if not _llm_router_enabled():
        return None
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        from apps.document.utils.client_openia import generate_chat_completion
        from apps.document.utils.llm import ROLE_FAST, resolve_model

        messages = [
            {"role": "system", "content": _LLM_ROUTER_SYSTEM_PROMPT},
            {"role": "user", "content": raw[:1200]},
        ]
        body, _usage = generate_chat_completion(
            messages,
            model=os.environ.get("RAG_ROUTER_MODEL") or resolve_model(ROLE_FAST),
            temperature=0.0,
            max_tokens=80,
            timeout=8,
        )
        body = (body or "").strip()
        match = re.search(r"\{[\s\S]*\}", body)
        if match:
            body = match.group(0)
        data = json.loads(body)
    except Exception as exc:
        logger.warning("LLM router failed, falling back to regex: %s", exc)
        return None

    query_type = str(data.get("query_type", "")).strip().lower()
    coverage_mode = str(data.get("coverage_mode", "")).strip().lower()
    is_general_raw = data.get("is_general")
    confidence = str(data.get("confidence", "medium")).strip().lower()

    valid_types = {QUERY_TYPE_FACTUAL, QUERY_TYPE_NUMERIC,
                   QUERY_TYPE_EXTRACTION_PER_ENTITY,
                   QUERY_TYPE_COMPARATIVE, QUERY_TYPE_PANORAMA}
    valid_coverage = {COVERAGE_MODE_FOCUSED, COVERAGE_MODE_BALANCED, COVERAGE_MODE_ALL}
    valid_confidence = {CLASSIFIER_CONFIDENCE_HIGH, CLASSIFIER_CONFIDENCE_MEDIUM,
                        CLASSIFIER_CONFIDENCE_LOW}

    if query_type not in valid_types:
        logger.warning("LLM router returned invalid query_type=%r", query_type)
        return None
    if query_type == QUERY_TYPE_EXTRACTION_PER_ENTITY:
        # Per-entity extraction must cover every document.
        coverage_mode = COVERAGE_MODE_ALL
    elif coverage_mode not in valid_coverage:
        # Backfill coverage from query_type if missing/invalid.
        coverage_mode = (
            COVERAGE_MODE_BALANCED
            if query_type in {QUERY_TYPE_PANORAMA, QUERY_TYPE_COMPARATIVE}
            else COVERAGE_MODE_FOCUSED
        )

    norm = _normalize(raw)
    out = QueryAnalysis(raw_text=raw, normalized=norm)
    out.keywords = _extract_keywords(norm)
    out.numeric_tokens = _extract_numeric_tokens(norm)
    out.query_type = query_type
    out.coverage_mode = coverage_mode
    out.locality, out.operation = _locality_operation(query_type)
    if isinstance(is_general_raw, bool):
        out.is_general = is_general_raw
    else:
        out.is_general = query_type in {
            QUERY_TYPE_PANORAMA,
            QUERY_TYPE_COMPARATIVE,
            QUERY_TYPE_EXTRACTION_PER_ENTITY,
        }
    out.sub_queries = _heuristic_sub_queries(out)
    out.classifier_source = CLASSIFIER_SOURCE_LLM
    out.classifier_confidence = (
        confidence if confidence in valid_confidence else CLASSIFIER_CONFIDENCE_MEDIUM
    )
    return out


def classify_query_hybrid(text: str) -> QueryAnalysis:
    """
    Hybrid classifier: regex first; if the regex is confident, return it.
    Otherwise call the LLM router. On any LLM failure, fall back to regex.

    This is the production-facing classifier — call this from the RAG
    pipeline instead of ``classify_query`` directly so we get the
    auto-routing behaviour and the per-decision telemetry.
    """
    regex_analysis = classify_query(text)
    # Empty text or trivially short queries: trust the regex (no LLM call).
    if not regex_analysis.normalized:
        return regex_analysis
    if regex_analysis.classifier_confidence == CLASSIFIER_CONFIDENCE_HIGH:
        return regex_analysis
    # Skip the LLM for very short queries (cheaper, regex tends to win).
    if len(regex_analysis.normalized.split()) < 5:
        return regex_analysis
    if not _llm_router_enabled():
        return regex_analysis

    llm_analysis = classify_query_llm(text)
    if llm_analysis is None:
        return regex_analysis

    # Preserve the regex keyword/numeric extraction (they are deterministic
    # over the raw text and useful for downstream lexical retrieval).
    llm_analysis.keywords = regex_analysis.keywords
    llm_analysis.numeric_tokens = regex_analysis.numeric_tokens
    return llm_analysis


def apply_response_mode_override(
    analysis: QueryAnalysis,
    response_mode: str | None,
) -> QueryAnalysis:
    """
    Deterministic override from explicit frontend selector.
    If provided, this mode takes precedence over heuristic / LLM
    classification.
    """
    mode = (response_mode or "").strip().lower()
    if not mode:
        return analysis

    if mode == RESPONSE_MODE_PUNTUAL:
        analysis.query_type = QUERY_TYPE_FACTUAL
        analysis.coverage_mode = COVERAGE_MODE_FOCUSED
        analysis.is_general = False
        analysis.sub_queries = []
        analysis.classifier_source = CLASSIFIER_SOURCE_OVERRIDE
        analysis.classifier_confidence = CLASSIFIER_CONFIDENCE_HIGH
        return analysis

    if mode == RESPONSE_MODE_PANORAMA:
        analysis.query_type = QUERY_TYPE_PANORAMA
        analysis.coverage_mode = COVERAGE_MODE_ALL
        analysis.is_general = True
        analysis.sub_queries = _heuristic_sub_queries(analysis)
        analysis.classifier_source = CLASSIFIER_SOURCE_OVERRIDE
        analysis.classifier_confidence = CLASSIFIER_CONFIDENCE_HIGH
        return analysis

    if mode == RESPONSE_MODE_COMPARACION:
        analysis.query_type = QUERY_TYPE_COMPARATIVE
        analysis.coverage_mode = COVERAGE_MODE_BALANCED
        analysis.is_general = True
        analysis.sub_queries = _heuristic_sub_queries(analysis)
        analysis.classifier_source = CLASSIFIER_SOURCE_OVERRIDE
        analysis.classifier_confidence = CLASSIFIER_CONFIDENCE_HIGH
        return analysis

    if mode == RESPONSE_MODE_EXTRACCION:
        analysis.query_type = QUERY_TYPE_NUMERIC
        analysis.coverage_mode = COVERAGE_MODE_FOCUSED
        analysis.is_general = False
        analysis.sub_queries = []
        analysis.classifier_source = CLASSIFIER_SOURCE_OVERRIDE
        analysis.classifier_confidence = CLASSIFIER_CONFIDENCE_HIGH
        return analysis

    if mode == RESPONSE_MODE_TABLA:
        analysis.query_type = QUERY_TYPE_NUMERIC
        analysis.coverage_mode = COVERAGE_MODE_FOCUSED
        analysis.is_general = False
        analysis.sub_queries = []
        analysis.classifier_source = CLASSIFIER_SOURCE_OVERRIDE
        analysis.classifier_confidence = CLASSIFIER_CONFIDENCE_HIGH
        return analysis

    return analysis


def _heuristic_sub_queries(analysis: QueryAnalysis) -> List[str]:
    """Cheap, language-agnostic decomposition.

    For panorama/comparative questions we synthesize a couple of focused
    sub-queries from extracted keywords so the retriever has multiple anchors.
    """
    if analysis.query_type not in {
        QUERY_TYPE_PANORAMA,
        QUERY_TYPE_COMPARATIVE,
        QUERY_TYPE_EXTRACTION_PER_ENTITY,
    }:
        return []
    if not analysis.keywords:
        return []
    # Group keywords in pairs to form mini-queries.
    kws = analysis.keywords[:6]
    pairs: list[str] = []
    for i in range(0, len(kws), 2):
        pair = " ".join(kws[i : i + 2])
        if pair:
            pairs.append(pair)
    return pairs[:3]


def expand_query_with_llm(
    analysis: QueryAnalysis,
    *,
    max_subqueries: int = 3,
    model: str | None = None,
) -> List[str]:
    """
    Use an LLM to produce focused sub-queries for broad questions.
    Disabled by default; opt-in via RAG_QUERY_EXPANSION_ENABLED=1.
    """
    if not is_query_expansion_enabled() or not analysis.is_general:
        return []
    try:
        from apps.document.utils.client_openia import generate_chat_completion

        prompt = (
            "Eres un asistente que genera sub-preguntas para mejorar un sistema RAG. "
            "A partir de la pregunta del usuario, devuelve un JSON array con "
            f"hasta {max_subqueries} sub-preguntas concretas, en el mismo idioma "
            "y orientadas a recuperar evidencias específicas en documentos. "
            "Si la pregunta ya es específica, devuelve []."
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": analysis.raw_text},
        ]
        text, _ = generate_chat_completion(
            messages,
            model=model or os.environ.get("RAG_QUERY_EXPANSION_MODEL", "gpt-4o-mini"),
            temperature=0.1,
            max_tokens=200,
            timeout=15,
        )
        text = text.strip()
        # Try direct JSON parse; fallback to extracting bracketed array.
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            text = match.group(0)
        items = json.loads(text)
        if isinstance(items, list):
            return [str(it).strip() for it in items if str(it).strip()][:max_subqueries]
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Query expansion failed, falling back to heuristics: %s", exc)
    return []


def contextualize_query(
    current_query: str,
    history: list[dict],
    *,
    model: str | None = None,
) -> str:
    """
    Rewrite a follow-up question as a standalone query using conversation
    history. If the question is already self-contained, returns it unchanged.

    ``history`` is a list of ``{"role": "user"|"assistant", "content": ...}``
    messages, most recent last. Only the last few turns are used.
    """
    text = (current_query or "").strip()
    if not text:
        return text

    if not history:
        return text

    recent = history[-4:]

    history_block = "\n".join(
        f"{'Usuario' if m['role'] == 'user' else 'Asistente'}: {(m.get('content') or '')[:300]}"
        for m in recent
    )

    try:
        from apps.document.utils.client_openia import generate_chat_completion

        messages = [
            {
                "role": "system",
                "content": (
                    "Tu tarea es reformular la última pregunta del usuario para que sea "
                    "una consulta de búsqueda autocontenida, incorporando contexto del "
                    "historial de conversación cuando sea necesario. "
                    "Si la pregunta ya es autocontenida, devuélvela tal cual. "
                    "Responde ÚNICAMENTE con la consulta reformulada, sin explicaciones."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Historial de conversación:\n{history_block}\n\n"
                    f"Última pregunta del usuario: {text}\n\n"
                    "Consulta reformulada:"
                ),
            },
        ]
        result, _ = generate_chat_completion(
            messages,
            model=model or os.environ.get("RAG_QUERY_REWRITE_MODEL", "gpt-4o-mini"),
            temperature=0.0,
            max_tokens=200,
            timeout=10,
        )
        rewritten = (result or "").strip()
        if rewritten:
            logger.debug("Query rewrite: %r -> %r", text, rewritten)
            return rewritten
    except Exception as exc:
        logger.warning("Query contextualization failed, using original: %s", exc)

    return text


def build_query_set(analysis: QueryAnalysis) -> List[str]:
    """
    Produce the final list of search queries to execute.
    Always includes the original query first.
    """
    queries: List[str] = []
    if analysis.raw_text:
        queries.append(analysis.raw_text)

    llm_subs = expand_query_with_llm(analysis)
    queries.extend(llm_subs)
    if not llm_subs:
        queries.extend(analysis.sub_queries)

    # Deduplicate while preserving order.
    seen: set[str] = set()
    deduped: List[str] = []
    for q in queries:
        key = _normalize(q)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(q)
    return deduped or ([analysis.raw_text] if analysis.raw_text else [])


# ---------------------------------------------------------------------------
# Retrieval plan — the shared "brain" contract (Phase 3)
# ---------------------------------------------------------------------------


@dataclass
class RetrievalPlan:
    """Concrete retrieval directives derived from a QueryAnalysis.

    This is the contract the executors consume (chat today; skills / evaluations
    as they adopt it) so retrieval strategy is decided in ONE place instead of
    being re-derived per stack.
    """

    strategy: str              # "global" | "hybrid_per_document"
    coverage_mode: str         # focused | balanced | all
    per_doc: int               # chunks to pull per document
    expand: bool               # apply small-to-big parent expansion
    model_role: str            # fast | balanced | deep (answer-model tier hint)
    per_document_answer: bool   # map-reduce: answer per document, then consolidate

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "coverage_mode": self.coverage_mode,
            "per_doc": self.per_doc,
            "expand": self.expand,
            "model_role": self.model_role,
            "per_document_answer": self.per_document_answer,
        }


def build_retrieval_plan(analysis: QueryAnalysis, *, doc_count: int = 1) -> RetrievalPlan:
    """Translate a QueryAnalysis (+ corpus size) into retrieval directives."""
    from apps.document.utils.llm import ROLE_BALANCED, ROLE_DEEP

    qt = analysis.query_type
    multi = doc_count > 1

    if (
        qt in {QUERY_TYPE_EXTRACTION_PER_ENTITY, QUERY_TYPE_PANORAMA, QUERY_TYPE_COMPARATIVE}
        or analysis.coverage_mode in {COVERAGE_MODE_ALL, COVERAGE_MODE_BALANCED}
    ):
        strategy = "hybrid_per_document"
    else:
        strategy = "global"

    per_doc = 2 if qt in {QUERY_TYPE_EXTRACTION_PER_ENTITY, QUERY_TYPE_COMPARATIVE} else 1

    # Deeper tier for distributed reasoning across many documents.
    if qt == QUERY_TYPE_COMPARATIVE or (
        qt in {QUERY_TYPE_EXTRACTION_PER_ENTITY, QUERY_TYPE_PANORAMA}
        and multi
        and doc_count >= 5
    ):
        model_role = ROLE_DEEP
    else:
        model_role = ROLE_BALANCED

    per_document_answer = qt == QUERY_TYPE_EXTRACTION_PER_ENTITY and multi

    return RetrievalPlan(
        strategy=strategy,
        coverage_mode=analysis.coverage_mode,
        per_doc=per_doc,
        expand=True,
        model_role=model_role,
        per_document_answer=per_document_answer,
    )


def plan_for_query(
    text: str,
    *,
    response_mode: str | None = None,
    doc_count: int = 1,
) -> tuple[QueryAnalysis, RetrievalPlan]:
    """Single shared entry point: classify a query and produce its retrieval plan.

    Use this from chat, skills and evaluations so the routing decision is made in
    one place. ``response_mode`` (an explicit frontend selector) deterministically
    overrides the classifier.
    """
    analysis = classify_query_hybrid(text)
    analysis = apply_response_mode_override(analysis, response_mode)
    plan = build_retrieval_plan(analysis, doc_count=doc_count)
    return analysis, plan


def _auto_strategy_enabled() -> bool:
    return os.environ.get("RAG_AUTO_STRATEGY", "1").strip().lower() in (
        "1", "true", "yes", "on",
    )


def recommend_strategy(query_text: str, *, default: str = "global") -> str:
    """Cheap (regex-only, no LLM) retrieval-strategy recommendation for the
    skills and evaluations stacks.

    Returns ``"hybrid_per_document"`` when the query is *distributed* (per-entity
    / comparative / panorama / all-coverage) — i.e. it benefits from per-document
    recall — otherwise ``default``. The strategy decision is independent of the
    corpus size, so callers don't need to pass a document count.
    """
    if not _auto_strategy_enabled():
        return default
    plan = build_retrieval_plan(classify_query(query_text or ""), doc_count=2)
    return "hybrid_per_document" if plan.strategy == "hybrid_per_document" else default
