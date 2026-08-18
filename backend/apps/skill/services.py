from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import List

from django.db.models import Count, Max, QuerySet
from django.utils import timezone

from apps.chat.services.rag import (
    RAG_MIN_SIMILARITY,
    _chunk_similarity,
    build_context_block,
    fetch_relevant_chunks,
    retrieve_for_chat,
)
from apps.chat.services.query_analysis import recommend_strategy
from apps.chat.services.retrieval import lexical_search, rrf_fuse
from apps.document.models import Document
from apps.document.utils.client_openia import generate_chat_completion, generate_with_tools
from apps.document.utils.llm import (
    ROLE_BALANCED,
    effective_chat_model,
    tool_capable_model,
)
from apps.skill import context_budget
from apps.skill.models import (
    ExecutionOutputMode,
    ExecutionStatus,
    RetrievalStrategy,
    SkillExecution,
    SkillStep,
    SkillStepType,
    SkillTier,
    SkillType,
    StepEvidenceMode,
)
from apps.skill.table_schema import schema_has_columns

logger = logging.getLogger(__name__)


class StepAwaitingApproval(Exception):
    """
    Raised by _run_copilot when a step with approval_required=True has been
    completed and persisted. The runner catches this and sets status=AWAITING_APPROVAL
    instead of FAILED.
    """

DEFAULT_CHUNKS = int(os.environ.get("SKILL_CONTEXT_CHUNKS", "6"))

# Per-step context budget for Copilot workflows. Larger than DEFAULT_CHUNKS so
# each section is authored from substantially more evidence, producing more
# complete, professional deliverables. Kept separate from DEFAULT_CHUNKS so
# synchronous quick skills are not slowed down.
COPILOT_STEP_CHUNKS = int(os.environ.get("SKILL_COPILOT_STEP_CHUNKS", "12"))

# Historial: cuántas secciones previas viajan completas y con cuánto presupuesto
# se compacta el resto. Son variables de entorno y no campos del modelo porque
# todavía no sabemos el valor correcto: conviene poder ajustarlo sin migrar.
HISTORY_FULL_STEPS = int(os.environ.get("SKILL_HISTORY_FULL_STEPS", "2"))
HISTORY_SUMMARY_CHARS = int(os.environ.get("SKILL_HISTORY_SUMMARY_CHARS", "1200"))


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# Contexto-primero: mandar los documentos, no fragmentos de los documentos.
# Es una variable y no una constante porque invierte el camino principal del
# motor: si una corrida sale mal, se vuelve al esquema anterior apagando esto
# en la task definition, sin desplegar.
def is_context_first_enabled() -> bool:
    return _flag("SKILL_CONTEXT_FIRST", "1")


# Subconjunto de los diagnósticos de recuperación que se persiste por paso.
# `retrieve_for_chat` calcula bastante más, pero esto es lo que permite explicar
# por qué un paso recibió la evidencia que recibió — o por qué no recibió nada.
RETRIEVAL_DIAGNOSTIC_KEYS = (
    "retrieval_mode", "retrieval_skipped_reason", "query_type", "coverage_mode",
    "vector_candidates", "lexical_candidates", "fused_candidates",
    "final_chunks", "unique_documents", "retrieval_confidence",
    "max_similarity", "retrieval_timed_out", "coverage_met",
)

# Maximum chunks assembled from the research phase scratchpad.
RESEARCH_SCRATCHPAD_MAX_CHUNKS = int(os.environ.get("SKILL_RESEARCH_SCRATCHPAD_CHUNKS", "20"))
# Maximum distinct research queries derived automatically from step instructions.
RESEARCH_AUTO_QUERIES_MAX = int(os.environ.get("SKILL_RESEARCH_AUTO_QUERIES", "8"))

# Quality bar injected into each authored (non-table) Copilot step so the
# output reads like a professional consulting deliverable rather than a terse
# answer. Traceability is surfaced via clickable source chips in the UI, so we
# ask the model to name source documents in prose instead of emitting literal
# [#N] markers (which the result view renders as plain text).
COPILOT_DELIVERABLE_STANDARD = (
    "## Estándar de entregable profesional\n"
    "- Redactá esta sección como parte de un entregable de consultoría en sostenibilidad/ESG: "
    "completa, precisa y bien estructurada.\n"
    "- Desarrollá el análisis en profundidad; usá subtítulos, párrafos y listas o tablas markdown "
    "cuando aporten claridad. Evitá respuestas superficiales o de una sola línea.\n"
    "- Fundamentá cada afirmación en la evidencia documental provista; cuando uses un dato puntual, "
    "mencioná el documento del que proviene por su nombre. No inventes datos ni rellenes con "
    "generalidades no sustentadas.\n"
    "- Si la evidencia es insuficiente para algún punto, declaralo explícitamente en lugar de inferir."
)


# ---------------------------------------------------------------------------
# Document resolver
# ---------------------------------------------------------------------------

def resolve_documents(execution: SkillExecution) -> QuerySet[Document]:
    """
    Returns the queryset of documents available for this execution context.
    - Repository: only is_active=True documents
    - Project: all linked documents
    - Document: the single document

    When ``execution.metadata["document_slugs_filter"]`` contains slugs, the
    base context queryset is intersected with that selection so the run only
    sees the documents the user explicitly chose. An empty/absent filter
    preserves the legacy behaviour of using the full context.
    """
    metadata = execution.metadata or {}
    slug_filter = (
        metadata.get("document_slugs_filter")
        or execution.skill.pinned_document_slugs
        or []
    )
    slug_filter = [s for s in slug_filter if isinstance(s, str) and s.strip()]

    if execution.repository_id:
        from apps.repository.models import RepositoryDocument
        doc_ids = (
            RepositoryDocument.objects
            .filter(repository_id=execution.repository_id, is_active=True)
            .values_list("document_id", flat=True)
        )
        qs = Document.objects.filter(id__in=doc_ids)
        if slug_filter:
            qs = qs.filter(slug__in=slug_filter)
        return qs

    if execution.project_id:
        from apps.project.models import ProjectDocument
        doc_ids = (
            ProjectDocument.objects
            .filter(project_id=execution.project_id)
            .values_list("document_id", flat=True)
        )
        qs = Document.objects.filter(id__in=doc_ids)
        if slug_filter:
            qs = qs.filter(slug__in=slug_filter)
        return qs

    if execution.document_id:
        return Document.objects.filter(id=execution.document_id)

    return Document.objects.none()


def build_document_snapshot(documents: QuerySet[Document]) -> list:
    """
    Identidad y huella de cada documento del alcance de la corrida.

    Además del identificador va una huella del texto indexado
    (``chunk_count`` + ``last_chunk_id``), que cambia en cuanto el documento se
    reprocesa. Sin ella, dos corridas "sobre los mismos documentos" pueden en
    realidad haber leído textos distintos y no habría manera de detectarlo al
    comparar los resultados.

    Ordenado por id para que el snapshot de dos corridas sea comparable
    directamente, sin depender del orden en que la base devuelva las filas.
    """
    rows = (
        documents.annotate(
            chunk_count=Count("chunks"),
            last_chunk_id=Max("chunks__id"),
        )
        .values(
            "id", "slug", "name", "chunking_status",
            "page_count", "chunk_count", "last_chunk_id",
        )
        .order_by("id")
    )
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Manifiesto de corrida
# ---------------------------------------------------------------------------

RUN_MANIFEST_SCHEMA = 1


def _definition_fingerprint(skill, steps: List[SkillStep]) -> str:
    """
    Huella de la definición del workflow tal como se ejecutó.

    Dos corridas solo son comparables si corrieron la misma definición. Como
    todavía no hay versionado de skills, esta huella es lo que permite darse
    cuenta de que alguien editó un paso entre una corrida y la siguiente, en
    vez de atribuirle la diferencia al modelo.
    """
    payload = {
        "tier": skill.tier,
        "system_prompt": skill.system_prompt,
        "prompt_template": skill.prompt_template,
        "temperature": skill.temperature,
        "comparative_mode_enabled": skill.comparative_mode_enabled,
        "strict_missing_evidence": skill.strict_missing_evidence,
        "research_phase_enabled": skill.research_phase_enabled,
        "research_queries": list(skill.research_queries or []),
        "tools_enabled": skill.tools_enabled,
        "retrieval": {
            "strategy": skill.retrieval_strategy,
            "query_template": skill.retrieval_query_template,
            "k_per_doc": skill.k_per_doc,
            "total_limit": skill.total_limit,
            "max_per_doc_after_rerank": skill.max_per_doc_after_rerank,
        },
        "steps": [
            {
                "position": step.position,
                "tier": step.tier,
                "evidence_mode": step.evidence_mode,
                "title": step.title,
                "instructions": step.instructions,
                "step_type": step.step_type,
                "linked_skill_id": step.linked_skill_id,
                "document_slugs": list(step.document_slugs or []),
                "output_mode": step.output_mode,
                "table_schema": step.table_schema or {},
                "approval_required": step.approval_required,
            }
            for step in steps
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _retrieval_runtime_config() -> dict:
    """
    Los flags que deciden *qué fragmentos ve el modelo*, tal como estaban.

    Se leen de los módulos que realmente los aplican, no se reimplementan acá:
    un default que se desincronice haría que el manifiesto mienta justo sobre
    lo que se quiere auditar.
    """
    from apps.chat.services import rag as rag_module
    from apps.chat.services.context_builder import is_mmr_enabled
    from apps.chat.services.query_analysis import (
        is_llm_router_enabled,
        is_query_expansion_enabled,
    )
    from apps.chat.services.reranker import is_reranker_enabled

    return {
        "llm_router_enabled": is_llm_router_enabled(),
        "query_expansion_enabled": is_query_expansion_enabled(),
        "reranker_enabled": is_reranker_enabled(),
        "rerank_pool": rag_module.RAG_RERANK_POOL,
        "min_similarity": rag_module.RAG_MIN_SIMILARITY,
        "recall_mode": rag_module.RAG_RECALL_MODE,
        "parent_expansion": rag_module.RAG_PARENT_EXPANSION,
        "parent_window": rag_module.RAG_PARENT_WINDOW,
        "mmr_enabled": is_mmr_enabled(),
        "copilot_step_chunks": COPILOT_STEP_CHUNKS,
        "default_chunks": DEFAULT_CHUNKS,
        # Cómo llegó la base documental al modelo. Es lo primero que hay que
        # mirar al comparar dos corridas: bajo contexto-primero la mayoría de
        # los flags de arriba no se aplican a ningún paso.
        "context_first": is_context_first_enabled(),
        "context_window": context_budget.CONTEXT_WINDOW,
        "context_safety_margin": context_budget.CONTEXT_SAFETY_MARGIN,
        "degraded_doc_tokens": context_budget.DEGRADED_DOC_TOKENS,
        "chars_per_token": context_budget.CHARS_PER_TOKEN,
        "context_cache_ttl": context_budget.CACHE_TTL or "default",
    }


def build_run_manifest(execution: SkillExecution, steps: List[SkillStep]) -> dict:
    """
    Todo lo que hace falta para afirmar que dos corridas tuvieron el mismo input.

    Deliberadamente **no** incluye los documentos: esos viven en
    ``execution.document_snapshot``, que es un campo de primera clase del
    modelo y ya lleva la huella de cada uno. Duplicarlos acá solo abriría la
    puerta a que las dos copias se contradigan.
    """
    metadata = execution.metadata or {}
    skill = execution.skill
    return {
        "schema": RUN_MANIFEST_SCHEMA,
        "provider": os.environ.get("LLM_PROVIDER", "openai").strip().lower(),
        "skill": {
            "slug": skill.slug,
            "type": skill.skill_type,
            "tier": skill.tier,
            "model_configured": skill.model,
            "temperature": skill.temperature,
        },
        "definition_fingerprint": _definition_fingerprint(skill, steps),
        "retrieval": _retrieval_runtime_config(),
        "scope": {
            "document_slugs_filter": list(metadata.get("document_slugs_filter") or []),
            "step_document_overrides": dict(metadata.get("step_document_overrides") or {}),
            "pinned_document_slugs": list(skill.pinned_document_slugs or []),
        },
        "input_values": dict(execution.input_values or {}),
        "extra_instructions": execution.extra_instructions or "",
    }


def _preserved_metadata(execution: SkillExecution, *, models_used) -> dict:
    """
    Claves de ``metadata`` que tienen que sobrevivir a la escritura final.

    Los runners rearman ``metadata`` desde cero al terminar. Sin esto se
    perdían el alcance documental que eligió el usuario al lanzar y el
    manifiesto de la corrida — es decir, exactamente lo que después hace falta
    para saber sobre qué se corrió. Una ejecución completada quedaba sin
    registro de su propio input.
    """
    metadata = execution.metadata or {}
    manifest = dict(metadata.get("run_manifest") or {})
    if manifest:
        manifest["models_used"] = sorted({m for m in models_used if m})
    return {
        "run_manifest": manifest,
        "document_slugs_filter": list(metadata.get("document_slugs_filter") or []),
        "step_document_overrides": dict(metadata.get("step_document_overrides") or {}),
        "review_each_step": bool(metadata.get("review_each_step")),
        "table_columns": metadata.get("table_columns", []),
        "table_schema": metadata.get("table_schema", {}),
    }


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

def _render_prompt_variables(
    template: str,
    *,
    context_block: str,
    extra_instructions: str,
    input_values: dict,
) -> str:
    """
    Replace all template tokens in ``template``.

    Token resolution order:
    1. {{context}}            — RAG context block.
    2. {{extra_instructions}} — free-text user override.
    3. {{key}}                — typed SkillParameter values from input_values.
    """
    result = template
    result = result.replace("{{context}}", context_block or "(No document content found)")
    result = result.replace("{{extra_instructions}}", extra_instructions or "")
    for key, value in (input_values or {}).items():
        result = result.replace(f"{{{{{key}}}}}", str(value) if value is not None else "")
    return result.strip()


# Backward-compatible wrapper used by existing tests.
def _render_quick_prompt(template: str, context_block: str, extra_instructions: str) -> str:
    return _render_prompt_variables(
        template,
        context_block=context_block,
        extra_instructions=extra_instructions,
        input_values={},
    )


def _comparative_instruction_block(strict_missing_evidence: bool) -> str:
    lines = [
        "Comparative output requirements:",
        "1) Present findings by document first for each criterion.",
        "2) For every criterion, include every active document even if there is no direct evidence.",
    ]
    if strict_missing_evidence:
        lines.append(
            "3) If a document does not contain evidence for a criterion, explicitly write: "
            "'Sin evidencia en fuentes provistas'."
        )
    else:
        lines.append(
            "3) If evidence is missing, state that limitation clearly and avoid unsupported inference."
        )
    return "\n".join(lines).strip()


def _chunks_to_sources(chunks) -> list[dict]:
    """
    Serialize a list of chunks into the citation-friendly ``sources`` shape the
    frontend uses to render clickable source chips (slug + chunk index resolve
    the vector/chunk viewer modal). De-duplicated by (slug, chunk_index).
    """
    sources: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for c in chunks:
        document = getattr(c, "document", None)
        if document is None:
            continue
        key = (document.slug, c.chunk_index)
        if key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "document_slug": document.slug,
                "document_name": document.name,
                "chunk_index": c.chunk_index,
            }
        )
    return sources


def _collect_source_stats_from_sources(sources: list[dict], total_docs: int) -> dict:
    """Cobertura documental a partir de las fuentes ya serializadas.

    ``chunks_per_document`` sigue contando solo fragmentos —es lo que la
    interfaz usa para el visor— pero la cobertura cuenta documentos, que es lo
    que la pregunta "¿leyó todo el expediente?" quiere saber.
    """
    docs_covered: set[str] = set()
    chunks_per_document: dict[str, int] = {}
    documents_delivered_full: set[str] = set()
    for source in sources:
        slug = source.get("document_slug")
        if not slug:
            continue
        docs_covered.add(slug)
        if source.get("delivery") == context_budget.FULL:
            documents_delivered_full.add(slug)
        if source.get("chunk_index") is not None:
            chunks_per_document[slug] = chunks_per_document.get(slug, 0) + 1
    return {
        "docs_total": total_docs,
        "docs_covered": len(docs_covered),
        "doc_coverage_ratio": (
            round(len(docs_covered) / total_docs, 4) if total_docs else 0
        ),
        "docs_delivered_full": sorted(documents_delivered_full),
        "chunks_per_document": chunks_per_document,
    }


def _collect_source_stats(chunks, total_docs: int) -> dict:
    docs_covered = set()
    chunks_per_document = {}
    for c in chunks:
        docs_covered.add(c.document.slug)
        chunks_per_document[c.document.slug] = chunks_per_document.get(c.document.slug, 0) + 1
    return {
        "docs_total": total_docs,
        "docs_covered": len(docs_covered),
        "doc_coverage_ratio": round((len(docs_covered) / total_docs), 4) if total_docs else 0,
        "chunks_per_document": chunks_per_document,
    }


# ---------------------------------------------------------------------------
# Table prompt + validation helpers (reused by Quick and Copilot steps)
# ---------------------------------------------------------------------------

def build_table_system_prompt(base_system_prompt: str, table_schema: dict) -> str:
    """
    Build the system prompt that forces the model to emit JSON matching the
    expected table schema, including per-column hints when provided.
    """
    columns = table_schema.get("columns") or []
    columns_json = json.dumps(columns, ensure_ascii=False)
    column_instructions = []
    for column in columns:
        prompt_hint = (column.get("prompt_hint") or "").strip()
        if not prompt_hint:
            continue
        column_instructions.append(
            f"- {column.get('key')} ({column.get('type')}): {prompt_hint}"
        )
    column_instructions_block = (
        "\n".join(column_instructions) or "- No extra per-column hints."
    )
    return (
        f"{base_system_prompt}\n\n"
        "Debes responder EXCLUSIVAMENTE en JSON válido (sin markdown, sin texto adicional) "
        "con este schema:\n"
        '{"type":"table","columns":[string],"rows":[object]}\n'
        "Usa EXACTAMENTE estas columnas y metadatos en este orden: "
        f"{columns_json}. "
        "Cada fila debe incluir todas las columnas con su tipo esperado."
        "\n\nInstrucciones por columna:\n"
        f"{column_instructions_block}"
    )


def coerce_table_output(*, output_text: str, table_schema: dict) -> dict:
    """
    Parse and normalize a model's tabular JSON response against the schema.

    Returns:
        {
            "type": "table",
            "columns": [str],          # ordered keys
            "column_schema": [dict],   # full column metadata
            "rows": [dict],            # normalized rows keyed by column key
        }
    """
    schema_columns = table_schema.get("columns") or []
    normalized_keys = [c.get("key") for c in schema_columns if c.get("key")]
    if not normalized_keys or not schema_columns:
        raise ValueError("Missing required columns for table output.")

    try:
        parsed = json.loads((output_text or "").strip())
    except json.JSONDecodeError as exc:
        raise ValueError("Model returned invalid JSON for table output.") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Table output must be a JSON object.")
    rows = parsed.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Table output must contain a 'rows' array.")

    type_map = {c["key"]: c.get("type", "text") for c in schema_columns}
    required_map = {c["key"]: bool(c.get("required", False)) for c in schema_columns}
    enum_map = {c["key"]: set(c.get("allowed_values") or []) for c in schema_columns}

    normalized_rows: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized_row = {}
        for col in normalized_keys:
            value = row.get(col, None)
            value = normalize_table_cell_value(
                value=value,
                col_type=type_map.get(col, "text"),
                required=required_map.get(col, False),
                allowed_values=enum_map.get(col, set()),
            )
            normalized_row[col] = value
        normalized_rows.append(normalized_row)

    return {
        "type": "table",
        "columns": normalized_keys,
        "column_schema": schema_columns,
        "rows": normalized_rows,
    }


def normalize_table_cell_value(*, value, col_type: str, required: bool, allowed_values: set):
    """Best-effort normalization of a model-generated cell value to the column type."""
    if value in (None, ""):
        return ""
    if col_type == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "si", "sí"}:
            return True
        if text in {"false", "0", "no"}:
            return False
        return ""
    if col_type == "number":
        try:
            number = float(value)
            if number.is_integer():
                return int(number)
            return number
        except (TypeError, ValueError):
            return ""
    if col_type == "enum":
        text = str(value).strip()
        if text in allowed_values:
            return text
        lowered = {str(v).lower(): v for v in allowed_values}
        mapped = lowered.get(text.lower())
        return mapped if mapped is not None else ""
    return str(value).strip()


def _table_summary_for_history(title: str, table: dict) -> str:
    """Compact representation of a tabular step output for follow-up steps."""
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    return f"### {title}\n[Tabla generada con {len(rows)} fila(s) y columnas: {', '.join(columns)}]"


# ---------------------------------------------------------------------------
# Tool-aware completion helper
# ---------------------------------------------------------------------------

def _with_operation_context(base_system_prompt: str, execution: SkillExecution) -> str:
    """
    Antepone al system prompt el contexto de la operación de la ejecución.

    Un workflow de CAF corre siempre sobre una operación, y el modelo tiene
    que saber de cuál se trata en todos los pasos: país, monto, objetivo,
    componentes y el documento principal. Va en el system prompt —y no en el
    prompt de cada paso— para que sea exactamente el mismo a lo largo de todo
    el workflow.

    Se calcula una sola vez por corrida y se guarda en la propia ejecución,
    que vive lo que dura el proceso: un workflow de diez pasos, si no, pegaría
    diez veces contra la base para reconstruir el mismo texto.

    Sin operación (ejecuciones sobre repositorio o documento suelto) devuelve
    el prompt intacto.
    """
    cached = getattr(execution, "_operation_context_block", None)
    if cached is None:
        from apps.skill.operation_context import build_operation_context_block

        cached = build_operation_context_block(execution.project)
        execution._operation_context_block = cached

    if not cached:
        return base_system_prompt
    return f"{base_system_prompt}\n\n{cached}"


def resolve_tier(skill, step: SkillStep | None = None) -> str:
    """
    Capacidad efectiva: la del paso si la declara, si no la del workflow.

    Los pasos de un mismo informe no piden lo mismo. Describir el marco de
    políticas de un país a partir de sus documentos es extracción; integrar
    tres criterios técnicos en una determinación es juicio. Que el tier sea
    por paso permite pagar capacidad donde hace falta en vez de elegir un
    modelo para el peor caso y correr los diecisiete ahí.

    Los valores de ``SkillTier`` coinciden a propósito con los roles de
    ``apps.document.utils.llm``, así que esto se pasa tal cual como ``role``.
    """
    if step is not None and step.tier:
        return step.tier
    return skill.tier or SkillTier.BALANCED


def _resolve_model(skill, tier: str) -> str:
    """El modelo que va a atender esta llamada.

    Existe aparte de ``_call_model`` porque hay que saberlo *antes* de armar el
    pedido: el corpus documental viaja como bloques con punto de caché si el
    proveedor es Anthropic, y como texto inline si no. Resolverlo en dos
    lugares distintos sería la forma segura de que un día dejen de coincidir.
    """
    role = tier or ROLE_BALANCED
    if skill.tools_enabled:
        return tool_capable_model(skill.model, role)
    return effective_chat_model(skill.model, role)


def _call_model(
    messages: list[dict],
    *,
    skill,
    tier: str,
    tool_ctx=None,
) -> tuple[str, dict, str]:
    """
    Dispatch to generate_with_tools or generate_chat_completion depending on
    whether the skill has tools_enabled and a valid tool context is provided.

    Devuelve ``(texto, usage, modelo)``. El modelo se devuelve —en vez de
    recalcularlo en el llamador— porque es el único punto donde se sabe cuál
    se usó de verdad: ``skill.model`` es solo el valor de partida y el tier lo
    resuelve el provider en tiempo de request. Una ejecución que no registre
    esto no permite distinguir después en qué modelo corrió.
    """
    role = tier or ROLE_BALANCED

    if skill.tools_enabled and tool_ctx is not None:
        from apps.skill.tools import ALL_TOOLS, execute_tool

        def _executor(name: str, args_json: str) -> str:
            return execute_tool(name, args_json, tool_ctx)

        model = _resolve_model(skill, role)
        text, usage = generate_with_tools(
            messages,
            tools=ALL_TOOLS,
            tool_executor=_executor,
            model=model,
            temperature=skill.temperature,
        )
        return text, usage, model

    model = effective_chat_model(skill.model, role)
    text, usage = generate_chat_completion(
        messages,
        model=model,
        temperature=skill.temperature,
    )
    return text, usage, model


# ---------------------------------------------------------------------------
# Research phase  (Sprint 2A)
# ---------------------------------------------------------------------------

def _run_research_phase(
    *,
    execution: SkillExecution,
    skill,
    documents: QuerySet[Document],
    steps: List[SkillStep],
) -> tuple[str, list]:
    """
    Broad retrieval pass before the authoring steps.

    One global pgvector query + one lexical query per research query,
    then a single RRF pass cross all queries so chunks that appear in
    multiple searches float up and duplicates are naturally eliminated.

    Returns (scratchpad_block, chunks_used).
    """
    research_queries: list[str] = list(skill.research_queries or [])

    if not research_queries:
        seen: set[str] = set()
        for step in steps:
            q = f"{step.title}. {step.instructions}"[:200].strip()
            if q and q not in seen:
                seen.add(q)
                research_queries.append(q)
            if len(research_queries) >= RESEARCH_AUTO_QUERIES_MAX:
                break

    if not research_queries:
        return "", []

    # Phase 5: each research query goes through the unified engine; the per-query
    # ranked lists are still RRF-fused for cross-query breadth.
    all_ranked_lists: list[list] = []
    for query in research_queries[:RESEARCH_AUTO_QUERIES_MAX]:
        try:
            r = retrieve_for_chat(
                user=execution.owner,
                query_text=query,
                allowed_documents=documents,
                top_n=6,
                total_limit=6,
                retrieval_strategy="global",
            )
            if r.chunks:
                all_ranked_lists.append(list(r.chunks))
        except Exception as exc:
            logger.warning("Research phase query %r failed: %s", query, exc)

    if not all_ranked_lists:
        return "", []

    fused = rrf_fuse(all_ranked_lists, top_n=RESEARCH_SCRATCHPAD_MAX_CHUNKS)
    final = [
        c for c in fused
        if _chunk_similarity(c) is None or _chunk_similarity(c) >= RAG_MIN_SIMILARITY
    ]

    if not final:
        return "", []

    return build_context_block(final), final


# ---------------------------------------------------------------------------
# Quick skill runner helpers
# ---------------------------------------------------------------------------

def _build_skill_coverage_instruction(
    documents: "QuerySet[Document]", chunks: list
) -> str:
    """
    Coverage policy block for comparative-mode quick skills.
    Only meaningful with 2+ documents — returns empty string otherwise.
    """
    doc_list = list(documents.values("id", "name", "slug").order_by("name"))
    total = len(doc_list)
    if total <= 1:
        return ""
    covered_ids = {c.document_id for c in chunks}
    missing = [d for d in doc_list if d["id"] not in covered_ids]
    covered_count = total - len(missing)
    missing_text = (
        " Documentos sin evidencia recuperada: "
        + ", ".join(f"{d['name']} ({d['slug']})" for d in missing)
        + "."
        if missing
        else ""
    )
    return (
        "\n\nPOLÍTICA DE COBERTURA OBLIGATORIA:\n"
        f"- Esta skill tiene {total} documentos en scope.\n"
        f"- El contexto recuperado cubre {covered_count}/{total} documentos.\n"
        "- Debes incluir una entrada por cada documento cubierto.\n"
        "- Si un documento no tiene evidencia para un criterio, indícalo explícitamente "
        "como 'Sin evidencia en fuentes provistas'."
        f"{missing_text}"
    )


# ---------------------------------------------------------------------------
# Quick skill runner
# ---------------------------------------------------------------------------

def _resolve_skill_strategy(skill_obj, query_text: str) -> str:
    """Effective retrieval strategy for a skill (Phase 3 brain adoption).

    Priority: comparative_mode → an explicitly non-default strategy → the shared
    classifier's recommendation (auto-upgrade distributed tasks to per-document).
    Set RAG_AUTO_STRATEGY=0 to disable the auto-upgrade.
    """
    if skill_obj.comparative_mode_enabled:
        return RetrievalStrategy.HYBRID_PER_DOCUMENT
    if skill_obj.retrieval_strategy != RetrievalStrategy.GLOBAL:
        return skill_obj.retrieval_strategy
    if recommend_strategy(query_text) == "hybrid_per_document":
        return RetrievalStrategy.HYBRID_PER_DOCUMENT
    return RetrievalStrategy.GLOBAL


def _run_quick(execution: SkillExecution, documents: QuerySet[Document]) -> None:
    from apps.skill.tools import SkillToolContext

    skill = execution.skill

    # 1. Build the retrieval query — use explicit template when configured,
    #    otherwise fall back to the old metadata-based construction.
    auto_query = f"{skill.name}. {skill.description}. {execution.extra_instructions}".strip()
    if skill.retrieval_query_template:
        retrieval_query = skill.retrieval_query_template.replace(
            "{{extra_instructions}}", execution.extra_instructions or ""
        ).strip() or auto_query
    else:
        retrieval_query = auto_query

    effective_retrieval_strategy = _resolve_skill_strategy(skill, retrieval_query)

    # 2-6. Unified retrieval (Phase 5): delegate to the shared engine
    # (retrieve_for_chat) so skills get F1 recall + parent expansion + the
    # Phase-3 plan instead of re-implementing vector + lexical + RRF + threshold.
    retrieval = retrieve_for_chat(
        user=execution.owner,
        query_text=retrieval_query,
        allowed_documents=documents,
        top_n=DEFAULT_CHUNKS,
        total_limit=skill.total_limit,
        max_chunks_per_doc=skill.max_per_doc_after_rerank,
        k_per_doc=skill.k_per_doc,
        retrieval_strategy=effective_retrieval_strategy,
    )
    final_chunks = list(retrieval.chunks)
    context_block = retrieval.context_block or build_context_block(final_chunks)
    vector_count = retrieval.diagnostics.get("vector_candidates", 0)
    lexical_count = retrieval.diagnostics.get("lexical_candidates", 0)

    prompt = _render_prompt_variables(
        skill.prompt_template,
        context_block=context_block,
        extra_instructions=execution.extra_instructions,
        input_values=execution.input_values,
    )
    if skill.comparative_mode_enabled:
        prompt = (
            f"{prompt}\n\n{_comparative_instruction_block(skill.strict_missing_evidence)}"
        ).strip()
        coverage = _build_skill_coverage_instruction(documents, final_chunks)
        if coverage:
            prompt = f"{prompt}\n{coverage}".strip()

    is_table = execution.output_mode == ExecutionOutputMode.TABLE
    table_schema = execution.metadata.get("table_schema") or {}

    base_system_prompt = _with_operation_context(skill.system_prompt, execution)
    system_prompt = (
        build_table_system_prompt(base_system_prompt, table_schema)
        if is_table
        else base_system_prompt
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    tool_ctx = SkillToolContext(user=execution.owner, allowed_documents=documents)
    tier_used = resolve_tier(skill)
    output_text, usage, model_used = _call_model(
        messages, skill=skill, tier=tier_used, tool_ctx=tool_ctx
    )
    all_chunks = final_chunks + tool_ctx.additional_chunks

    if is_table:
        parsed = coerce_table_output(output_text=output_text, table_schema=table_schema)
        execution.output = ""
        execution.output_structured = parsed
    else:
        execution.output = output_text
        execution.output_structured = {}

    source_stats = _collect_source_stats(all_chunks, total_docs=documents.count())
    execution.metadata = {
        "usage": usage,
        "chunks_used": len(all_chunks),
        "vector_chunks": vector_count,
        "lexical_chunks": lexical_count,
        "retrieval_query": retrieval_query,
        "tool_calls_made": len(tool_ctx.additional_chunks) > 0,
        "comparative_mode_enabled": skill.comparative_mode_enabled,
        "strict_missing_evidence": skill.strict_missing_evidence,
        "retrieval_strategy_used": effective_retrieval_strategy,
        **source_stats,
        "sources": [
            {
                "document_slug": c.document.slug,
                "document_name": c.document.name,
                "chunk_index": c.chunk_index,
            }
            for c in all_chunks
        ],
        **_preserved_metadata(execution, models_used={model_used}),
    }


# ---------------------------------------------------------------------------
# Copilot skill runner
# ---------------------------------------------------------------------------

def _resolve_step_output_config(step: SkillStep) -> tuple[str, dict]:
    """
    Resolve the effective output mode and table schema for a single step.

    Steps default to TEXT. If the step is configured with TABLE but no
    table_schema, the step is downgraded to text instead of failing — this
    keeps the runner forgiving for legacy data while warnings are surfaced
    in metadata.
    """
    output_mode = step.output_mode or ExecutionOutputMode.TEXT
    table_schema = step.table_schema or {}
    if output_mode == ExecutionOutputMode.TABLE and not schema_has_columns(table_schema):
        return ExecutionOutputMode.TEXT, {}
    return output_mode, table_schema


def _rebuild_previous_sections(step_results: list[dict]) -> list[tuple[str, str]]:
    """
    Reconstruye ``(título, cuerpo)`` de los pasos ya completados.

    Se guardan en crudo y se rinden recién al armar el prompt: así el mismo
    historial puede entrar completo para los pasos recientes y compactado para
    los viejos, sin tener que reconstruirlo dos veces.
    """
    sections: list[tuple[str, str]] = []
    for entry in step_results:
        title = entry.get("title", "")
        if entry.get("output_mode") == ExecutionOutputMode.TABLE and "table" in entry:
            sections.append((title, _table_summary_for_history(title, entry["table"])))
        else:
            sections.append((title, entry.get("content", "")))
    return sections


def _compact_section(title: str, body: str, *, max_chars: int) -> str:
    """
    Una sección previa reducida a lo que los pasos siguientes necesitan de ella.

    Conserva el primer párrafo —que enmarca— y todos los últimos que entren en
    el presupuesto, porque en un entregable de consultoría la determinación vive
    al final. Lo del medio es desarrollo: si un paso posterior necesita un dato
    puntual, la fuente son los documentos y no la prosa de otra sección.
    """
    body = (body or "").strip()
    if len(body) <= max_chars:
        return f"### {title}\n{body}"

    paragraphs = [p.strip() for p in body.split("\n\n") if p.strip()]
    if len(paragraphs) < 2:
        return f"### {title}\n{body[:max_chars].rstrip()}\n[…]"

    head, rest = paragraphs[0], paragraphs[1:]
    tail: list[str] = []
    used = len(head)
    for paragraph in reversed(rest):
        if used + len(paragraph) > max_chars:
            break
        tail.insert(0, paragraph)
        used += len(paragraph)

    kept = [head]
    if len(tail) < len(rest):
        kept.append("[…]")
    kept.extend(tail)
    return f"### {title}\n" + "\n\n".join(kept)


def _render_history(
    sections: list[tuple[str, str]], *, full_steps: int, max_chars: int
) -> list[str]:
    """
    Las secciones previas tal como las ve el paso actual.

    Las últimas ``full_steps`` van completas para que no se corte el hilo
    narrativo; las anteriores, compactadas. En la corrida 313, con las
    diecisiete completas, el historial acumulado fue el 56% de los tokens de
    entrada — más de seis veces lo que ocupó la evidencia documental. Ese
    presupuesto es el que hace falta para mandar los documentos.
    """
    cutoff = len(sections) - max(0, full_steps)
    rendered: list[str] = []
    for index, (title, body) in enumerate(sections):
        if index >= cutoff:
            rendered.append(f"### {title}\n{body}")
        else:
            rendered.append(_compact_section(title, body, max_chars=max_chars))
    return rendered


def _resolve_step_documents(
    step: SkillStep,
    documents: QuerySet[Document],
    runtime_override_slugs: list[str] | None = None,
) -> QuerySet[Document]:
    """
    Narrow the execution's document scope to a single step's documents.

    Resolution order (first non-empty wins):
    1. **Runtime overrides** — per-step slugs the user chose when launching the
       workflow (stored in ``execution.metadata["step_document_overrides"]``).
    2. **Definition-time slugs** — ``step.document_slugs`` set in the skill
       builder.
    3. **Full context** — all documents from the execution's repository/project.

    An empty list at any level means "not specified" and falls through to the
    next level.
    """
    # Pick the first non-empty slug list.
    effective_slugs: list[str] = []
    if runtime_override_slugs:
        effective_slugs = [s for s in runtime_override_slugs if isinstance(s, str) and s.strip()]
    if not effective_slugs:
        effective_slugs = [s for s in (step.document_slugs or []) if isinstance(s, str) and s.strip()]
    if not effective_slugs:
        return documents

    scoped = documents.filter(slug__in=effective_slugs)
    # Defensive: if the slugs reference docs not in the context (e.g. removed
    # after the workflow was authored), fall back to the full context so the
    # step still produces something rather than running on an empty corpus.
    #
    # La caída se avisa. Antes era silenciosa, y bajo contexto-primero eso pasó
    # de ser un detalle a ser el problema: un paso que el autor acotó a un
    # documento termina recibiendo el expediente entero sin que nada lo diga.
    if scoped.exists():
        return scoped
    logger.warning(
        "El paso %s referencia documentos que no están en el alcance (%s); "
        "se usa el contexto completo.",
        step.id,
        ", ".join(effective_slugs),
    )
    return documents


# ---------------------------------------------------------------------------
# Contexto-primero
# ---------------------------------------------------------------------------

def _load_document_texts(documents, cache: dict[int, str]) -> dict[int, str]:
    """Texto extraído de cada documento, leído una sola vez por corrida.

    Se cachea por ejecución porque el mismo expediente vuelve en cada paso: sin
    esto, diecisiete pasos releen del disco el mismo millón de caracteres.
    """
    missing = [d.id for d in documents if d.id not in cache]
    if missing:
        for doc_id, text in Document.objects.filter(id__in=missing).values_list(
            "id", "extracted_text"
        ):
            cache[doc_id] = text or ""
    return cache


def _plan_sources(plan, chunks) -> list[dict]:
    """Las fuentes de un paso bajo contexto-primero.

    Un documento que viajó entero es una fuente aunque no haya fragmentos que
    mostrar: el registro de la corrida tiene que decir qué leyó el modelo, no
    qué trajo una búsqueda. Los fragmentos de los documentos degradados se
    agregan como hasta ahora, para que la interfaz siga resolviendo el visor.
    """
    sources: list[dict] = [
        {
            "document_slug": delivery.slug,
            "document_name": delivery.name,
            "chunk_index": None,
            "delivery": delivery.mode,
        }
        for delivery in plan.deliveries
    ]
    sources.extend(_chunks_to_sources(chunks))
    return sources


def _retrieve_partial_blocks(
    *, execution, plan, query_text: str, strategy: str | None
) -> tuple[dict[int, str], list, dict[str, str]]:
    """Recuperación acotada a cada documento que no entró completo.

    Buscar dentro de un documento es un problema distinto —y mucho más
    tratable— que buscar en el expediente: no hay que decidir qué documento
    mira el paso, eso ya está decidido. El presupuesto tampoco se reparte: es
    el del documento degradado y de nadie más.
    """
    blocks: dict[int, str] = {}
    collected: list = []
    failures: dict[str, str] = {}
    for delivery in plan.degraded:
        wanted = context_budget.chunks_for_budget(delivery.tokens)
        try:
            retrieval = retrieve_for_chat(
                user=execution.owner,
                query_text=query_text,
                allowed_documents=Document.objects.filter(id=delivery.document.id),
                top_n=wanted,
                # Los tres topes van explícitos y en el mismo valor. Los
                # defaults reparten el presupuesto *entre* documentos —con uno
                # solo, el tope por documento cae a tres fragmentos— y acá el
                # documento es uno solo por definición: todo el presupuesto es
                # suyo. Sin esto, degradar un documento equivale a perderlo.
                total_limit=wanted,
                max_chunks_per_doc=wanted,
                k_per_doc=wanted,
                retrieval_strategy=strategy,
            )
            chunks = list(retrieval.chunks)
        except Exception as exc:
            # Si esto falla, el documento degradado llega vacío: el modelo ve
            # su nombre en el inventario y ni una línea de su contenido. Queda
            # anotado porque desde afuera es indistinguible de un documento que
            # simplemente no tenía nada relevante para la sección.
            logger.warning(
                "Falló la recuperación dentro de %s: %s", delivery.slug, exc
            )
            failures[delivery.slug] = str(exc)[:200]
            continue
        if not chunks:
            failures[delivery.slug] = "sin fragmentos relevantes"
            continue
        collected.extend(chunks)
        blocks[delivery.document.id] = build_context_block(chunks)
    return blocks, collected, failures


def build_step_corpus(
    *,
    execution,
    step_documents,
    query_text: str,
    reserved_tokens: int,
    blueprint_id: int | None,
    document_texts: dict[int, str],
    strategy: str | None = None,
    retrieve_partials: bool = True,
) -> tuple[str, str, list, "context_budget.ContextPlan"]:
    """La base documental de un paso, tal como la va a ver el modelo.

    Está afuera de ``_run_copilot`` para que se pueda armar sin ejecutar el
    workflow: ``manage.py preview_workflow_context`` la usa para mostrar qué
    recibe cada paso antes de gastar una corrida en averiguarlo. La pregunta
    "¿qué le estamos mandando al modelo?" no debería requerir mandárselo.

    Devuelve ``(estable, variable, fragmentos, plan)``. La separación entre lo
    estable y lo variable no es organizativa: es dónde va el punto de caché.
    Todo lo que cambie de un paso a otro tiene que quedar del lado de afuera,
    o el corpus se paga entero diecisiete veces.
    """
    planned_documents = list(step_documents.defer("extracted_text"))
    _load_document_texts(planned_documents, document_texts)
    plan = context_budget.plan_context(
        planned_documents,
        reserved_tokens=reserved_tokens,
        blueprint_id=blueprint_id,
        texts=document_texts,
    )
    if retrieve_partials:
        partial_blocks, chunks, failures = _retrieve_partial_blocks(
            execution=execution, plan=plan, query_text=query_text, strategy=strategy
        )
        plan.partial_failures = failures
    else:
        partial_blocks, chunks = {}, []
    stable = "\n\n".join(
        (
            context_budget.render_inventory(plan),
            context_budget.render_corpus(plan, texts=document_texts),
        )
    )
    volatile = context_budget.render_partials(plan, partial_blocks=partial_blocks)
    return stable, volatile, chunks, plan


def _run_skill_ref_step(
    *,
    execution: SkillExecution,
    step: SkillStep,
    step_documents: QuerySet[Document],
    previous_sections: list[tuple[str, str]],
) -> tuple[dict, dict, list]:
    """
    Execute a workflow step that delegates to an existing QUICK skill.

    Mirrors the quick-skill retrieval + prompt pipeline using the *linked*
    skill's configuration, scoped to this step's documents. The output is
    folded into the workflow as a regular text step entry.

    Returns ``(step_entry, usage, chunks)``.
    """
    from apps.skill.tools import SkillToolContext

    linked = step.linked_skill
    if linked is None:
        # Should not happen (serializer enforces it) but stay defensive.
        return (
            {
                "step_id": step.id,
                "title": step.title,
                "output_mode": ExecutionOutputMode.TEXT,
                "content": "_(Skill referenciada no disponible.)_",
            },
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            [],
        )

    # Build retrieval query from the linked skill's config (+ run extras).
    extra = execution.extra_instructions or ""
    if linked.retrieval_query_template:
        retrieval_query = linked.retrieval_query_template.replace(
            "{{extra_instructions}}", extra
        ).strip()
    else:
        retrieval_query = f"{linked.name}. {linked.description}. {extra}".strip()

    effective_strategy = _resolve_skill_strategy(linked, retrieval_query)

    # Unified retrieval (Phase 5): delegate to the shared engine.
    retrieval = retrieve_for_chat(
        user=execution.owner,
        query_text=retrieval_query,
        allowed_documents=step_documents,
        top_n=DEFAULT_CHUNKS,
        total_limit=linked.total_limit,
        max_chunks_per_doc=linked.max_per_doc_after_rerank,
        k_per_doc=linked.k_per_doc,
        retrieval_strategy=effective_strategy,
    )
    final_chunks = list(retrieval.chunks)
    context_block = retrieval.context_block or build_context_block(final_chunks)

    prompt = _render_prompt_variables(
        linked.prompt_template,
        context_block=context_block,
        extra_instructions=extra,
        input_values=execution.input_values,
    )
    if linked.comparative_mode_enabled:
        prompt = (
            f"{prompt}\n\n{_comparative_instruction_block(linked.strict_missing_evidence)}"
        ).strip()

    # Give the linked skill awareness of the workflow context so its output
    # connects with the sections written before it.
    if previous_sections:
        prior = "\n".join(
            _render_history(previous_sections[-2:], full_steps=HISTORY_FULL_STEPS,
                            max_chars=HISTORY_SUMMARY_CHARS)
        )
        prompt = f"{prompt}\n\n## Secciones previas del documento:\n{prior}"

    messages = [
        {"role": "system", "content": _with_operation_context(linked.system_prompt, execution)},
        {"role": "user", "content": prompt},
    ]
    tool_ctx = SkillToolContext(user=execution.owner, allowed_documents=step_documents)
    tier_used = resolve_tier(linked, step)
    content, usage, model_used = _call_model(
        messages, skill=linked, tier=tier_used, tool_ctx=tool_ctx
    )
    chunks = final_chunks + tool_ctx.additional_chunks

    step_entry = {
        "step_id": step.id,
        "title": step.title,
        "output_mode": ExecutionOutputMode.TEXT,
        "content": content,
        "via_skill": linked.slug,
        "via_skill_name": linked.name,
        "model": model_used,
        "tier": tier_used,
        "sources": _chunks_to_sources(chunks),
    }
    previous_sections.append((step.title, content))
    return step_entry, usage, chunks


def _run_copilot(execution: SkillExecution, documents: QuerySet[Document]) -> None:
    from apps.skill.tools import SkillToolContext

    skill = execution.skill
    steps: List[SkillStep] = list(skill.steps.all())
    if not steps:
        raise ValueError("This Copilot skill has no steps defined.")

    # Block-by-block confirmation: when the run requested it, pause after every
    # step for human review (the last step is exempt — there is nothing left to
    # gate, and the final draft is reviewed/edited in the completed view).
    review_each_step = bool((execution.metadata or {}).get("review_each_step"))
    last_step_position = steps[-1].position

    # Resume: load any steps already completed in a previous run segment.
    already_done: list[dict] = list(
        (execution.output_structured or {}).get("steps", [])
    )
    resume_from_position = len(already_done)

    step_results: list[dict] = list(already_done)
    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    # Rebuild conversation context from completed steps so later steps have full history.
    previous_sections: list[tuple[str, str]] = _rebuild_previous_sections(already_done)
    all_step_chunks: list = []
    effective_retrieval_strategy = _resolve_skill_strategy(
        skill,
        f"{skill.name}. {skill.description}. {execution.extra_instructions or ''}",
    )

    # Track chunk IDs already seen so each step can prioritise fresh coverage.
    seen_chunk_ids: set[int] = set()

    # Contexto-primero: estado compartido por toda la corrida.
    context_first = is_context_first_enabled()
    document_texts: dict[int, str] = {}
    blueprint_id = getattr(execution.project, "blueprint_document_id", None)

    # ------------------------------------------------------------------ #
    # Research phase (Sprint 2A)                                          #
    # ------------------------------------------------------------------ #
    shared_scratchpad = ""
    if skill.research_phase_enabled:
        shared_scratchpad, research_chunks = _run_research_phase(
            execution=execution,
            skill=skill,
            documents=documents,
            steps=steps,
        )
        all_step_chunks.extend(research_chunks)
        seen_chunk_ids.update(c.id for c in research_chunks)
        logger.debug(
            "Research phase for execution %s: %d chunks collected.",
            execution.id,
            len(research_chunks),
        )

    for step_index, step in enumerate(steps):
        # Skip steps already completed in a prior run segment (resume support).
        if step_index < resume_from_position:
            continue

        # Resolve the document scope for THIS step.  Priority:
        # 1. Runtime overrides from execution.metadata (user chose at launch)
        # 2. Definition-time step.document_slugs (set in skill builder)
        # 3. Full execution context
        runtime_overrides = (execution.metadata or {}).get("step_document_overrides", {})
        step_runtime_slugs = runtime_overrides.get(str(step.position), [])
        step_documents = _resolve_step_documents(step, documents, step_runtime_slugs)

        # ── Skill-reference step: delegate to an existing quick skill ──
        if step.step_type == SkillStepType.SKILL_REF and step.linked_skill_id:
            step_entry, usage, ref_chunks = _run_skill_ref_step(
                execution=execution,
                step=step,
                step_documents=step_documents,
                previous_sections=previous_sections,
            )
            all_step_chunks.extend(ref_chunks)
            seen_chunk_ids.update(c.id for c in ref_chunks if hasattr(c, "id"))
        else:
            # ── Instruction step: author content from the step's own prompt ──
            step_output_mode, step_table_schema = _resolve_step_output_config(step)
            is_table_step = step_output_mode == ExecutionOutputMode.TABLE

            # Con qué material trabaja este paso.
            evidence_mode = step.evidence_mode or StepEvidenceMode.BOTH
            wants_documents = evidence_mode != StepEvidenceMode.PREVIOUS
            wants_history = evidence_mode != StepEvidenceMode.DOCUMENTS

            # La consulta sale del paso y de nada más. Antes se le pegaba un
            # fragmento de la sección anterior para "encauzar" el embedding, y
            # con eso lo que el paso recuperaba dependía de lo que el modelo
            # había escrito recién: dos corridas divergían en el paso 3 y no
            # volvían a coincidir nunca. Un input del workflow no puede ser un
            # output del workflow.
            query_text = f"{step.title}. {step.instructions}".strip()

            step_diagnostics: dict = {}
            chunks: list = []
            context_block = ""
            plan = None
            # Contexto-primero arma el corpus recién después del prompt del
            # paso: el presupuesto documental es lo que sobra una vez contado
            # todo lo demás, así que hay que tener lo demás para poder contarlo.
            use_context_first = context_first and wants_documents

            if not wants_documents:
                step_diagnostics = {"retrieval_skipped_reason": "evidence_mode"}
            elif not use_context_first:
                # Camino anterior: fragmentos elegidos por la búsqueda sobre
                # todo el alcance del paso.
                try:
                    step_retrieval = retrieve_for_chat(
                        user=execution.owner,
                        query_text=query_text,
                        allowed_documents=step_documents,
                        top_n=COPILOT_STEP_CHUNKS,
                        total_limit=skill.total_limit,
                        max_chunks_per_doc=skill.max_per_doc_after_rerank,
                        k_per_doc=skill.k_per_doc,
                        retrieval_strategy=effective_retrieval_strategy,
                    )
                    chunks = list(step_retrieval.chunks)
                    step_diagnostics = {
                        key: step_retrieval.diagnostics[key]
                        for key in RETRIEVAL_DIAGNOSTIC_KEYS
                        if key in step_retrieval.diagnostics
                    }
                except Exception as exc:
                    logger.warning("Step %s retrieval failed: %s", step.id, exc)
                    step_diagnostics = {"retrieval_error": str(exc)[:200]}
                seen_chunk_ids.update(c.id for c in chunks)
                all_step_chunks.extend(chunks)
                context_block = build_context_block(chunks)

            # Compose the user prompt for this step
            lines = [
                f"## Task: {step.title}",
                "",
                f"Instructions: {step.instructions}",
            ]
            # Note the document scope so the model is aware it's narrowed.
            if step.document_slugs:
                lines.append(
                    "\n## Document scope: este paso analiza solo un subconjunto "
                    "de los documentos del contexto."
                )
            # Inject typed parameter values as a context note
            if execution.input_values:
                param_lines = [
                    f"- {k}: {v}"
                    for k, v in execution.input_values.items()
                    if v not in (None, "")
                ]
                if param_lines:
                    lines.append("\n## Run parameters:\n" + "\n".join(param_lines))

            if execution.extra_instructions:
                lines.append(f"\nAdditional instructions from user: {execution.extra_instructions}")
            if wants_history and previous_sections:
                lines.append("\n## Secciones previas de este informe:")
                lines.extend(
                    _render_history(
                        previous_sections,
                        full_steps=HISTORY_FULL_STEPS,
                        max_chars=HISTORY_SUMMARY_CHARS,
                    )
                )
            # Shared research scratchpad (Sprint 2A)
            if shared_scratchpad:
                lines.append(f"\n## Research scratchpad (broad corpus overview):\n{shared_scratchpad}")
            if context_block:
                lines.append(f"\n## Document context (targeted for this section):\n{context_block}")
            elif wants_documents and not use_context_first:
                # Sólo se avisa de la ausencia cuando el paso esperaba evidencia.
                # A un paso que integra resultados anteriores decirle que "no se
                # encontró contenido documental" lo empuja a declarar una carencia
                # que no existe.
                lines.append("\n(No document content found for this section — note this in your output.)")
            if skill.comparative_mode_enabled and not is_table_step:
                lines.append(
                    f"\n## Comparative constraints:\n{_comparative_instruction_block(skill.strict_missing_evidence)}"
                )
            # Professional deliverable standard — only for authored prose steps.
            # Table steps must return strict JSON, so we never relax their format.
            if not is_table_step:
                lines.append(f"\n{COPILOT_DELIVERABLE_STANDARD}")

            if use_context_first:
                lines.append(
                    "\nCeñite a la base documental listada al comienzo de este "
                    "mensaje y a sus reglas de uso."
                )

            prompt = "\n".join(lines)
            # El contexto de la operación va en el system prompt, no en el
            # prompt del paso: así es idéntico para todos los pasos del
            # workflow y queda como prefijo estable de la corrida.
            step_system_prompt = _with_operation_context(skill.system_prompt, execution)
            system_prompt = (
                build_table_system_prompt(step_system_prompt, step_table_schema)
                if is_table_step
                else step_system_prompt
            )

            tier_used = resolve_tier(skill, step)
            corpus_stable = ""
            corpus_volatile = ""
            if use_context_first:
                # El presupuesto documental es lo que queda de la ventana una
                # vez descontado todo lo que ya está comprometido. Se mide con
                # el prompt real del paso, no con un promedio: un paso con
                # secciones previas largas tiene menos lugar para documentos
                # que uno del principio, y esa diferencia es la que decide.
                reserved = (
                    context_budget.estimate_tokens(system_prompt)
                    + context_budget.estimate_tokens(prompt)
                    + context_budget.output_reserve()
                )
                corpus_stable, corpus_volatile, chunks, plan = build_step_corpus(
                    execution=execution,
                    step_documents=step_documents,
                    query_text=query_text,
                    reserved_tokens=reserved,
                    blueprint_id=blueprint_id,
                    document_texts=document_texts,
                    strategy=effective_retrieval_strategy,
                )
                seen_chunk_ids.update(c.id for c in chunks)
                all_step_chunks.extend(chunks)
                step_diagnostics = plan.diagnostics()

            messages = context_budget.build_messages(
                system_prompt=system_prompt,
                corpus_stable=corpus_stable,
                corpus_volatile=corpus_volatile,
                step_prompt=prompt,
                model=_resolve_model(skill, tier_used),
            )

            tool_ctx = SkillToolContext(user=execution.owner, allowed_documents=step_documents)
            content, usage, model_used = _call_model(
                messages, skill=skill, tier=tier_used, tool_ctx=tool_ctx
            )
            all_step_chunks.extend(tool_ctx.additional_chunks)

            step_sources = chunks + tool_ctx.additional_chunks
            step_entry: dict = {
                "step_id": step.id,
                "title": step.title,
                "output_mode": step_output_mode,
                "model": model_used,
                "tier": tier_used,
                "evidence_mode": evidence_mode,
                "retrieval": step_diagnostics,
                "sources": (
                    _plan_sources(plan, step_sources)
                    if plan is not None
                    else _chunks_to_sources(step_sources)
                ),
            }
            if is_table_step:
                try:
                    table = coerce_table_output(
                        output_text=content, table_schema=step_table_schema
                    )
                except ValueError as exc:
                    logger.warning(
                        "Step %s of execution %s produced invalid table JSON: %s",
                        step.id,
                        execution.id,
                        exc,
                    )
                    step_entry["output_mode"] = ExecutionOutputMode.TEXT
                    step_entry["content"] = content
                    step_entry["table_error"] = str(exc)
                    previous_sections.append((step.title, content))
                else:
                    step_entry["table"] = table
                    step_entry["content"] = ""
                    previous_sections.append(
                        (step.title, _table_summary_for_history(step.title, table))
                    )
            else:
                step_entry["content"] = content
                previous_sections.append((step.title, content))

        step_results.append(step_entry)

        for key in total_usage:
            total_usage[key] += usage.get(key, 0)

        # Persist this step immediately (Sprint 3: incremental output for polling).
        execution.output_structured = {"steps": step_results}
        execution.steps_completed = len(step_results)
        execution.save(update_fields=["output_structured", "steps_completed"])

        # Sprint 4 + block-by-block confirmation: pause for human review when the
        # step opts in via approval_required, OR when the run requested
        # review_each_step (every step except the last — see top of function).
        needs_review = step.approval_required or (
            review_each_step and step.position != last_step_position
        )
        if needs_review:
            execution.current_step_position = step.position
            execution.save(update_fields=["current_step_position"])
            raise StepAwaitingApproval(
                f"Step '{step.title}' (position {step.position}) is awaiting approval."
            )

    # Final metadata written once all steps complete.
    execution.output_structured = {"steps": step_results}
    # Aggregate global sources from each step's persisted sources rather than
    # all_step_chunks: on resumed (HITL) runs all_step_chunks only holds chunks
    # from the last segment, but step_results carries every step's sources.
    global_sources: list[dict] = []
    seen_sources: set[tuple] = set()
    for entry in step_results:
        for src in entry.get("sources", []):
            key = (src.get("document_slug"), src.get("chunk_index"))
            if key in seen_sources:
                continue
            seen_sources.add(key)
            global_sources.append(src)
    # La cobertura se calcula sobre las fuentes registradas y no sobre los
    # fragmentos: bajo contexto-primero un documento que viajó entero no deja
    # ni un fragmento detrás, y contarlo por fragmentos daría cobertura cero
    # justo en la corrida que leyó todo.
    source_stats = _collect_source_stats_from_sources(
        global_sources, total_docs=documents.count()
    )
    # Los modelos salen de `step_results` y no de una variable local porque en
    # una corrida reanudada los pasos previos los ejecutó otro segmento: la
    # variable local solo conoce el último tramo, el registro por paso conoce
    # la corrida entera.
    models_used = {entry.get("model") for entry in step_results}
    execution.metadata = {
        "usage": total_usage,
        "research_phase_enabled": skill.research_phase_enabled,
        "comparative_mode_enabled": skill.comparative_mode_enabled,
        "strict_missing_evidence": skill.strict_missing_evidence,
        "retrieval_strategy_used": effective_retrieval_strategy,
        **source_stats,
        "sources": global_sources,
        **_preserved_metadata(execution, models_used=models_used),
    }


# ---------------------------------------------------------------------------
# Backwards-compatible private aliases (kept for existing tests/imports)
# ---------------------------------------------------------------------------

_coerce_table_output = coerce_table_output
_normalize_table_cell_value = normalize_table_cell_value


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

class SkillRunner:
    def run(self, execution_id: int) -> SkillExecution:
        execution = (
            SkillExecution.objects
            .select_related("skill", "owner", "repository", "project", "document")
            .prefetch_related("skill__steps")
            .get(pk=execution_id)
        )

        if execution.status not in (
            ExecutionStatus.PENDING,
            ExecutionStatus.FAILED,
            ExecutionStatus.AWAITING_APPROVAL,
        ):
            return execution

        documents = resolve_documents(execution)
        if not documents.exists():
            execution.status = ExecutionStatus.FAILED
            execution.error_message = (
                "No documents found for this context. "
                "Make sure the repository/project has active sources."
            )
            execution.finished_at = timezone.now()
            execution.save(update_fields=["status", "error_message", "finished_at"])
            return execution

        execution.status = ExecutionStatus.RUNNING
        execution.started_at = timezone.now()
        execution.document_snapshot = build_document_snapshot(documents)
        execution.error_message = ""

        # El manifiesto se arma una sola vez, en el primer tramo. Una corrida
        # reanudada tras una aprobación no lo reescribe: lo que se quiere
        # registrar es el input con el que empezó. Si mientras tanto alguien
        # editó la definición, eso se anota en vez de pisarse en silencio —
        # es justo la diferencia que después explicaría dos salidas distintas.
        metadata = execution.metadata or {}
        steps = list(execution.skill.steps.all())
        manifest = build_run_manifest(execution, steps)
        existing = metadata.get("run_manifest") or {}
        if existing:
            if existing.get("definition_fingerprint") != manifest["definition_fingerprint"]:
                existing["definition_changed_during_run"] = True
                metadata["run_manifest"] = existing
        else:
            metadata["run_manifest"] = manifest
        execution.metadata = metadata

        execution.save(update_fields=[
            "status", "started_at", "document_snapshot", "error_message", "metadata",
        ])

        try:
            if execution.skill.skill_type == SkillType.QUICK:
                _run_quick(execution, documents)
            else:
                _run_copilot(execution, documents)
            execution.status = ExecutionStatus.COMPLETED
            execution.current_step_position = None
        except StepAwaitingApproval:
            # Not a failure — the run is intentionally paused.
            execution.status = ExecutionStatus.AWAITING_APPROVAL
        except Exception as exc:
            logger.exception("SkillExecution %s failed", execution.id)
            execution.status = ExecutionStatus.FAILED
            execution.error_message = str(exc)
        finally:
            execution.finished_at = timezone.now()
            # For copilot runs, output_structured was already persisted incrementally
            # per step — avoid a redundant final save of that field.
            if execution.skill.skill_type == SkillType.QUICK:
                execution.save(update_fields=[
                    "status", "output", "output_structured", "metadata",
                    "error_message", "finished_at", "current_step_position",
                ])
            else:
                execution.save(update_fields=[
                    "status", "metadata", "error_message",
                    "finished_at", "current_step_position",
                ])

        return execution


def execution_to_markdown(execution: SkillExecution) -> str:
    """Return the best available markdown representation of a skill execution."""
    if execution.edited_output and execution.edited_output.strip():
        return execution.edited_output.strip()

    if execution.skill.skill_type == SkillType.QUICK:
        if execution.output_mode == ExecutionOutputMode.TABLE:
            structured = execution.output_structured or {}
            columns = structured.get("columns") or []
            rows = structured.get("rows") or []
            if not columns:
                return execution.output or ""
            header = "| " + " | ".join(str(c) for c in columns) + " |"
            separator = "| " + " | ".join("---" for _ in columns) + " |"
            body = []
            for row in rows:
                if isinstance(row, dict):
                    body.append(
                        "| "
                        + " | ".join(str(row.get(col, "")) for col in columns)
                        + " |"
                    )
            return "\n".join([header, separator, *body])
        return execution.output or ""

    steps = (execution.output_structured or {}).get("steps") or []
    parts = []
    for step in steps:
        title = step.get("title") or "Step"
        if step.get("output_mode") == "table" and step.get("table"):
            table = step["table"]
            columns = table.get("columns") or []
            rows = table.get("rows") or []
            if columns:
                header = "| " + " | ".join(str(c) for c in columns) + " |"
                separator = "| " + " | ".join("---" for _ in columns) + " |"
                body = []
                for row in rows:
                    if isinstance(row, dict):
                        body.append(
                            "| "
                            + " | ".join(str(row.get(col, "")) for col in columns)
                            + " |"
                        )
                parts.append(f"## {title}\n\n" + "\n".join([header, separator, *body]))
            else:
                parts.append(f"## {title}\n\n{step.get('content') or ''}")
        else:
            parts.append(f"## {title}\n\n{step.get('content') or ''}")
    return "\n\n---\n\n".join(parts)


def execute_skill(execution: SkillExecution) -> SkillExecution:
    return SkillRunner().run(execution.id)


# ---------------------------------------------------------------------------
# Sprint 4 — Human-in-the-loop helpers
# ---------------------------------------------------------------------------

def approve_step(execution: SkillExecution, *, override_content: str | None = None) -> SkillExecution:
    """
    Approve the current awaiting step and continue the run.

    If ``override_content`` is provided the last completed step's text content
    is replaced before the run resumes. This lets the consultant edit the output
    and have subsequent steps use the corrected version as context.

    Returns the execution object (status will be PENDING until the async task
    picks it up and starts running again).
    """
    if execution.status != ExecutionStatus.AWAITING_APPROVAL:
        raise ValueError(
            f"Cannot approve: execution {execution.id} is not awaiting approval "
            f"(current status: {execution.status})."
        )

    if override_content is not None:
        steps = list((execution.output_structured or {}).get("steps", []))
        if steps:
            last = dict(steps[-1])
            # Only override text steps — table steps are left as-is.
            if last.get("output_mode") != ExecutionOutputMode.TABLE:
                last["content"] = override_content
                last["human_edited"] = True
                steps[-1] = last
                execution.output_structured = {"steps": steps}

    execution.status = ExecutionStatus.PENDING
    execution.error_message = ""
    execution.save(update_fields=["status", "output_structured", "error_message"])
    return execution


def regenerate_step(execution: SkillExecution) -> SkillExecution:
    """
    Discard the last completed step and re-run it.

    Strips the last step from output_structured, decrements steps_completed,
    and resets status to PENDING so the Celery task re-runs from that step.
    """
    if execution.status != ExecutionStatus.AWAITING_APPROVAL:
        raise ValueError(
            f"Cannot regenerate: execution {execution.id} is not awaiting approval "
            f"(current status: {execution.status})."
        )

    steps = list((execution.output_structured or {}).get("steps", []))
    if steps:
        steps.pop()
    execution.output_structured = {"steps": steps}
    execution.steps_completed = max(0, execution.steps_completed - 1)
    execution.status = ExecutionStatus.PENDING
    execution.current_step_position = None
    execution.error_message = ""
    execution.save(update_fields=[
        "status", "output_structured", "steps_completed",
        "current_step_position", "error_message",
    ])
    return execution
