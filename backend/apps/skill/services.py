from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
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
from apps.skill import definition as definition_module
from apps.skill.citations import citation_stats, resolve_citations
from apps.skill.models import (
    ExecutionOutputMode,
    OutputValidation,
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

# Estándar de entregable inyectado en cada paso de prosa de un copiloto.
#
# Hay dos, y la diferencia no es de estilo: es que describen situaciones
# distintas. Con recuperación, el modelo escribe una sección con seis mil
# tokens de fragmentos y el riesgo es que quede flaca — pedir profundidad es
# correcto. Con el expediente completo delante, el riesgo se invierte: el
# material sobra y la tentación es resumirlo todo. La misma instrucción que
# antes evitaba secciones pobres ahora empuja al relleno.
#
# Por eso el que corresponde se elige por situación en vez de borrar uno de los
# dos: el camino de recuperación sigue existiendo como plan B, y ahí la versión
# vieja sigue siendo la correcta.

_DELIVERABLE_BASE = (
    "## Estándar de entregable profesional\n"
    "- Redactá esta sección como parte de un entregable de consultoría en "
    "sostenibilidad/ESG para una banca de desarrollo: precisa, bien estructurada y "
    "escrita para alguien que va a tomar una decisión con ella.\n"
    "- Usá subtítulos, párrafos y listas o tablas markdown cuando aporten claridad.\n"
)

# Camino de recuperación: la evidencia es escasa y hay que aprovecharla.
COPILOT_DELIVERABLE_STANDARD = (
    _DELIVERABLE_BASE
    + "- Desarrollá el análisis en profundidad; evitá respuestas superficiales o de una "
    "sola línea.\n"
    "- Fundamentá cada afirmación en la evidencia documental provista; cuando uses un "
    "dato puntual, mencioná el documento del que proviene por su nombre. No inventes "
    "datos ni rellenes con generalidades no sustentadas.\n"
    "- Si la evidencia es insuficiente para algún punto, declaralo explícitamente en "
    "lugar de inferir."
)

# Contexto-primero: el material sobra, y lo que escasea es el criterio para
# elegir qué entra. Las reglas sobre qué se puede afirmar y qué se puede citar
# ya están en el inventario, con más precisión que acá — repetirlas sólo las
# diluye.
COPILOT_DELIVERABLE_STANDARD_CONTEXT_FIRST = (
    _DELIVERABLE_BASE
    + "- Tenés el expediente completo delante. El trabajo no es resumirlo: es elegir lo "
    "que decide esta sección y dejar afuera lo que no. Extensión proporcional a lo que "
    "el punto exige, no a lo que hay disponible.\n"
    "- Escribí sobre lo que encontraste, no sobre lo que buscaste. Nada de relatar el "
    "recorrido por los documentos ni de enumerar lo que revisaste."
)


def deliverable_standard(*, context_first: bool) -> str:
    """El estándar que corresponde a la situación del paso."""
    return (
        COPILOT_DELIVERABLE_STANDARD_CONTEXT_FIRST
        if context_first
        else COPILOT_DELIVERABLE_STANDARD
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

    Delega en ``apps.skill.definition``, que es donde vive la lista de campos que
    forman la definición. Antes esa lista estaba acá, escrita a mano y sin nada
    que la mantuviera al día: un campo nuevo en ``SkillStep`` no entraba en la
    huella y dos corridas con ese campo distinto se veían idénticas. Ahora la
    misma serialización que se guarda como versión es la que se hashea, así que
    huella y snapshot no pueden divergir.
    """
    return definition_module.fingerprint(
        definition_module.serialize_definition(skill, steps=steps)
    )


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
        # Las citas nativas son todo-o-nada por pedido, así que basta con
        # registrar si estaban activas: no hay estados intermedios que auditar.
        "citations_enabled": context_budget.are_citations_enabled(),
    }


def _blueprint_identity(execution: SkillExecution) -> dict | None:
    """Identidad del documento principal, para el manifiesto."""
    document = getattr(execution.project, "blueprint_document", None)
    if document is None:
        return None
    return {"id": document.id, "slug": document.slug, "name": document.name}


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
        # El número de versión es lo que se muestra y lo que permite recuperar la
        # definición entera; la huella queda porque es la identidad real y sirve
        # para comparar contra corridas viejas, anteriores al versionado.
        "definition_version": (
            execution.definition_version.version_number
            if execution.definition_version_id
            else None
        ),
        "retrieval": _retrieval_runtime_config(),
        "scope": {
            "document_slugs_filter": list(metadata.get("document_slugs_filter") or []),
            "step_document_overrides": dict(metadata.get("step_document_overrides") or {}),
            "pinned_document_slugs": list(skill.pinned_document_slugs or []),
            # Qué documento hizo de principal. El rol es estable —siempre el
            # documento de la operación— pero la identidad no: se sube una
            # versión nueva del IDO y cambia el sujeto de la auditoría. Dos
            # corridas con distinto principal no evalúan lo mismo, y sin esto
            # se verían comparables.
            "blueprint_document": _blueprint_identity(execution),
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
        # Parámetros que llegaron sin estar declarados. Es parte del registro del
        # input: si el prompt salió con un `{{token}}` sin resolver, esto es lo
        # que lo explica.
        "input_value_warnings": metadata.get("input_value_warnings", []),
        # Id de la corrida original, si esta es una repetición. Es lo que
        # ``compare_executions`` reporta como identidad de la comparación; sin
        # preservarlo, el runner lo pisaría al rearmar metadata desde cero.
        **({"rerun_of": metadata["rerun_of"]} if metadata.get("rerun_of") is not None else {}),
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


def _comparative_instruction_block(
    strict_missing_evidence: bool, *, has_inventory: bool = False
) -> str:
    """Requisitos de salida comparativa, según lo que el paso realmente tiene.

    Este bloque nació para forzar al RAG a cubrir todos los documentos: sin él,
    la recuperación traía tres de seis y la sección hablaba de tres. Cuando el
    alcance llega completo por construcción, esa parte sobra — y la fórmula
    "Sin evidencia en fuentes provistas" pasa a estorbar, porque aplasta en una
    sola frase los tres estados que el inventario acaba de distinguir: una
    ausencia real no es lo mismo que un documento mencionado que no tenemos.

    Se pregunta por el inventario y no por el plan a propósito: cuando se arma
    este bloque el plan todavía no existe —el presupuesto documental depende
    del prompt que estamos construyendo—, y de todos modos el inventario es
    más preciso que un booleano: dice documento por documento cuál llegó
    completo y cuál en fragmentos.
    """
    lines = ["Comparative output requirements:",
             "1) Present findings by document first for each criterion."]
    if not has_inventory:
        lines.append(
            "2) For every criterion, include every active document even if there is "
            "no direct evidence."
        )
        if strict_missing_evidence:
            lines.append(
                "3) If a document does not contain evidence for a criterion, explicitly "
                "write: 'Sin evidencia en fuentes provistas'."
            )
        else:
            lines.append(
                "3) If evidence is missing, state that limitation clearly and avoid "
                "unsupported inference."
            )
    else:
        lines.append(
            "2) Cubrí cada documento del inventario en cada criterio. "
            "Cuando un documento no diga nada sobre un criterio, usá el estado que "
            "corresponda de los tres del inventario — no una fórmula única."
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


class TableContractError(ValueError):
    """La salida no cumple el esquema declarado por el paso.

    Se distingue de ``ValueError`` a secas porque el motor la trata distinto:
    en modo estricto es motivo de reintento y, si el reintento tampoco cumple,
    de falla del paso. Un paso que declara una determinación auditable y no
    puede producirla tiene que decirlo, no escribir prosa.
    """


def coerce_table_output(
    *, output_text: str, table_schema: dict, strict: bool = False
) -> dict:
    """
    Parse and normalize a model's tabular JSON response against the schema.

    ``strict`` hace cumplir el contrato: una celda obligatoria vacía, un valor
    fuera del vocabulario declarado o una fila que no es un objeto levantan
    ``TableContractError`` en vez de convertirse en celda vacía. En modo
    tolerante nada falla, pero los problemas quedan registrados en ``issues``
    para que la corrida se pueda auditar después.

    Returns:
        {
            "type": "table",
            "columns": [str],          # ordered keys
            "column_schema": [dict],   # full column metadata
            "rows": [dict],            # normalized rows keyed by column key
            "issues": [dict],          # celdas que no cumplieron el contrato
        }
    """
    schema_columns = table_schema.get("columns") or []
    normalized_keys = [c.get("key") for c in schema_columns if c.get("key")]
    if not normalized_keys or not schema_columns:
        raise TableContractError("Missing required columns for table output.")

    try:
        parsed = json.loads((output_text or "").strip())
    except json.JSONDecodeError as exc:
        raise TableContractError("Model returned invalid JSON for table output.") from exc
    if not isinstance(parsed, dict):
        raise TableContractError("Table output must be a JSON object.")
    rows = parsed.get("rows")
    if not isinstance(rows, list):
        raise TableContractError("Table output must contain a 'rows' array.")

    type_map = {c["key"]: c.get("type", "text") for c in schema_columns}
    required_map = {c["key"]: bool(c.get("required", False)) for c in schema_columns}
    enum_map = {c["key"]: set(c.get("allowed_values") or []) for c in schema_columns}

    normalized_rows: list[dict] = []
    issues: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            if strict:
                raise TableContractError(
                    f"La fila {index} no es un objeto JSON."
                )
            issues.append({"row": index, "column": None, "problem": "row_not_object"})
            continue
        normalized_row = {}
        for col in normalized_keys:
            value, problem = validate_table_cell_value(
                value=row.get(col, None),
                col_type=type_map.get(col, "text"),
                required=required_map.get(col, False),
                allowed_values=enum_map.get(col, set()),
            )
            if problem:
                if strict:
                    raise TableContractError(
                        _cell_problem_message(
                            index, col, problem, row.get(col), enum_map.get(col, set())
                        )
                    )
                issues.append(
                    {
                        "row": index,
                        "column": col,
                        "problem": problem,
                        "received": row.get(col),
                    }
                )
            normalized_row[col] = value
        normalized_rows.append(normalized_row)

    return {
        "type": "table",
        "columns": normalized_keys,
        "column_schema": schema_columns,
        "rows": normalized_rows,
        "issues": issues,
    }


def _cell_problem_message(row: int, column: str, problem: str, received, allowed) -> str:
    """El mensaje que ve quien tiene que arreglar el paso — o el modelo, en el
    reintento. Dice qué llegó y qué se esperaba, no sólo que algo falló."""
    if problem == CELL_MISSING_REQUIRED:
        return f"Fila {row}: la columna obligatoria '{column}' vino vacía."
    if problem == CELL_INVALID_ENUM:
        opciones = ", ".join(sorted(allowed))
        return (
            f"Fila {row}: '{received}' no es un valor válido para '{column}'. "
            f"Valores permitidos: {opciones}."
        )
    tipo = {CELL_INVALID_NUMBER: "un número", CELL_INVALID_BOOLEAN: "un booleano"}
    return (
        f"Fila {row}: '{received}' no es {tipo.get(problem, 'un valor válido')} "
        f"para la columna '{column}'."
    )


# Por qué una celda quedó vacía. Sin esto, "el modelo no contestó" y "el modelo
# contestó algo fuera del vocabulario declarado" son el mismo string vacío — y
# al comparar dos corridas, una violación del contrato se ve igual que un dato
# que no estaba. Es la misma distinción que los tres estados del inventario,
# aplicada a la celda.
CELL_MISSING_REQUIRED = "missing_required"
CELL_INVALID_ENUM = "invalid_enum"
CELL_INVALID_NUMBER = "invalid_number"
CELL_INVALID_BOOLEAN = "invalid_boolean"


def validate_table_cell_value(
    *, value, col_type: str, required: bool, allowed_values: set
) -> tuple:
    """Normaliza una celda y devuelve ``(valor, problema)``.

    ``problema`` es ``None`` cuando la celda cumple el contrato. Cuando no,
    dice qué falló: es lo que permite que el modo estricto falle con un motivo
    y que el tolerante deje registro en vez de tragarse el error.
    """
    if value in (None, ""):
        return "", (CELL_MISSING_REQUIRED if required else None)

    if col_type == "boolean":
        if isinstance(value, bool):
            return value, None
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "si", "sí"}:
            return True, None
        if text in {"false", "0", "no"}:
            return False, None
        return "", CELL_INVALID_BOOLEAN

    if col_type == "number":
        try:
            number = float(value)
        except (TypeError, ValueError):
            return "", CELL_INVALID_NUMBER
        return (int(number) if number.is_integer() else number), None

    if col_type == "enum":
        text = str(value).strip()
        if text in allowed_values:
            return text, None
        # Coincidencia por mayúsculas: "ALINEADO" cuando el vocabulario dice
        # "Alineado" no es una violación del contrato, es formato.
        lowered = {str(v).lower(): v for v in allowed_values}
        mapped = lowered.get(text.lower())
        if mapped is not None:
            return mapped, None
        return "", CELL_INVALID_ENUM

    return str(value).strip(), None


def normalize_table_cell_value(*, value, col_type: str, required: bool, allowed_values: set):
    """Sólo el valor normalizado. Se conserva porque hay llamadores que no
    necesitan el diagnóstico."""
    normalized, _ = validate_table_cell_value(
        value=value, col_type=col_type, required=required, allowed_values=allowed_values
    )
    return normalized


def _table_summary_for_history(title: str, table: dict) -> str:
    """Compact representation of a tabular step output for follow-up steps."""
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    return f"### {title}\n[Tabla generada con {len(rows)} fila(s) y columnas: {', '.join(columns)}]"


# ---------------------------------------------------------------------------
# Tool-aware completion helper
# ---------------------------------------------------------------------------

def _with_operation_context(
    base_system_prompt: str,
    execution: SkillExecution,
    *,
    include_blueprint_summary: bool = True,
) -> str:
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
    attr = (
        "_operation_context_block"
        if include_blueprint_summary
        else "_operation_context_block_nosummary"
    )
    cached = getattr(execution, attr, None)
    if cached is None:
        from apps.skill.operation_context import build_operation_context_block

        cached = build_operation_context_block(
            execution.project, include_blueprint_summary=include_blueprint_summary
        )
        setattr(execution, attr, cached)

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
    citations_out: list | None = None,
) -> tuple[str, dict, str]:
    """
    Dispatch to generate_with_tools or generate_chat_completion depending on
    whether the skill has tools_enabled and a valid tool context is provided.

    ``citations_out`` recoge las citas nativas cuando el pedido lleva bloques
    ``document``. El camino con herramientas **no** las devuelve todavía: el
    bucle agéntico arma su propia conversación y las citas de las vueltas
    intermedias se perderían a medias, que es peor que no tenerlas. Un workflow
    con herramientas activadas corre sin citas y el manifiesto lo dice.

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
        citations_out=citations_out,
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

def is_strict_step(step: SkillStep) -> bool:
    """Si este paso hace cumplir el contrato que declara."""
    return (step.output_validation or OutputValidation.LENIENT) == OutputValidation.STRICT


def _resolve_step_output_config(step: SkillStep) -> tuple[str, dict]:
    """
    Resolve the effective output mode and table schema for a single step.

    Un paso que declara tabla y no trae esquema es una definición rota. En modo
    tolerante se degrada a texto —así los pasos viejos siguen corriendo— pero en
    modo estricto falla: degradar en silencio convierte una determinación
    auditable en prosa y la corrida termina sin que nadie se entere.
    """
    output_mode = step.output_mode or ExecutionOutputMode.TEXT
    table_schema = step.table_schema or {}
    if output_mode == ExecutionOutputMode.TABLE and not schema_has_columns(table_schema):
        if is_strict_step(step):
            raise TableContractError(
                f"El paso '{step.title}' declara salida tabular pero no define "
                "columnas. Cargá el esquema o cambiá la política del paso a "
                "'lenient'."
            )
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


@dataclass
class StepCorpus:
    """Lo que un paso le muestra al modelo, en las piezas en que viaja.

    Es un objeto y no una tupla porque las piezas ya son cinco y cada una tiene
    un rol distinto en el pedido: dos van antes del punto de caché, una después,
    y las otras dos no viajan —sirven para registrar la corrida—. Una tupla de
    cinco se desempaca mal una vez y el error es silencioso.
    """

    inventory: str
    documents: list  # context_budget.DocumentPayload
    volatile: str
    chunks: list
    plan: "context_budget.ContextPlan"


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
) -> "StepCorpus":
    """La base documental de un paso, tal como la va a ver el modelo.

    Está afuera de ``_run_copilot`` para que se pueda armar sin ejecutar el
    workflow: ``manage.py preview_workflow_context`` la usa para mostrar qué
    recibe cada paso antes de gastar una corrida en averiguarlo. La pregunta
    "¿qué le estamos mandando al modelo?" no debería requerir mandárselo.

    La separación entre lo estable —inventario y documentos— y lo variable
    —los fragmentos— no es organizativa: es dónde va el punto de caché. Todo
    lo que cambie de un paso a otro tiene que quedar del lado de afuera, o el
    corpus se paga entero diecisiete veces.
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
    return StepCorpus(
        inventory=context_budget.render_inventory(plan),
        documents=context_budget.build_document_payloads(plan, texts=document_texts),
        volatile=context_budget.render_partials(plan, partial_blocks=partial_blocks),
        chunks=chunks,
        plan=plan,
    )


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


def _coerce_with_retry(
    *,
    content: str,
    table_schema: dict,
    strict: bool,
    messages: list[dict],
    skill,
    tier: str,
    tool_ctx,
    step,
    execution,
    citations_out: list | None = None,
) -> tuple[dict, str, dict]:
    """Valida la tabla y, si no cumple, le da al modelo una segunda oportunidad.

    Un reintento, y sólo en modo estricto. La razón es de reproducibilidad: sin
    él, un paso de diecisiete que devuelve una coma de más tira la corrida
    entera o —peor, como era antes— la convierte en prosa y sigue. Con él, el
    modelo recibe qué falló y qué se esperaba, que es información que no tenía.

    El reintento no reformula la tarea ni afloja el contrato: repite el mismo
    pedido agregando el error concreto. Si tampoco cumple, el paso falla.

    ``citations_out`` trae las citas del primer intento y, si hay reintento, se
    reemplaza por las del segundo. Sin eso el paso quedaba con las citas de una
    respuesta que se descartó: las filas venían del reintento y las fuentes de
    lo anterior, sin que nada lo delatara.

    Devuelve ``(tabla, contenido_usado, usage_del_reintento)``.
    """
    sin_usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    try:
        return (
            coerce_table_output(
                output_text=content, table_schema=table_schema, strict=strict
            ),
            content,
            sin_usage,
        )
    except ValueError as exc:
        # TableContractError hereda de ValueError, así que esto cubre tanto una
        # celda fuera del contrato como un JSON que no parsea.
        if not strict:
            raise
        # El mensaje se guarda acá: Python borra la variable del `except` al
        # salir del bloque, y abajo hace falta para decirle al modelo qué falló.
        motivo = str(exc)
        logger.warning(
            "Paso %s de la ejecución %s no cumplió el contrato de tabla (%s). Reintento.",
            step.id,
            execution.id,
            motivo,
        )

    correccion = (
        f"{content}\n\n"
        "La respuesta anterior no cumple el esquema declarado para este paso:\n"
        f"{motivo}\n\n"
        "Devolvé únicamente el JSON corregido, respetando exactamente las columnas "
        "y los valores permitidos. No expliques el error ni agregues texto fuera "
        "del JSON."
    )
    reintento = list(messages) + [{"role": "user", "content": correccion}]
    citas_reintento: list[dict] = []
    nuevo_contenido, usage, _ = _call_model(
        reintento,
        skill=skill,
        tier=tier,
        tool_ctx=tool_ctx,
        citations_out=citas_reintento,
    )
    if citations_out is not None:
        # Las del primer intento describen un texto que ya no existe, así que se
        # reemplazan enteras en vez de acumularse.
        citations_out.clear()
        citations_out.extend(citas_reintento)
    # Si el segundo intento tampoco cumple, la excepción sube y el paso falla:
    # dos intentos contra un contrato explícito es suficiente evidencia de que
    # el problema no es el modelo teniendo un mal día.
    return (
        coerce_table_output(
            output_text=nuevo_contenido, table_schema=table_schema, strict=True
        ),
        nuevo_contenido,
        usage,
    )


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
            # Bajo contexto-primero el inventario ya lista, uno por uno, los
            # documentos que el paso tiene. Este aviso decía lo mismo peor: "un
            # subconjunto" sin decir de qué.
            if step.document_slugs and not use_context_first:
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
                    "\n## Comparative constraints:\n"
                    + _comparative_instruction_block(
                        skill.strict_missing_evidence,
                        has_inventory=use_context_first,
                    )
                )
            # Professional deliverable standard — only for authored prose steps.
            # Table steps must return strict JSON, so we never relax their format.
            if not is_table_step:
                lines.append(
                    f"\n{deliverable_standard(context_first=use_context_first)}"
                )

            if use_context_first:
                lines.append(
                    "\nCeñite a la base documental listada al comienzo de este "
                    "mensaje y a sus reglas de uso."
                )

            prompt = "\n".join(lines)
            # El contexto de la operación va en el system prompt, no en el
            # prompt del paso: así es idéntico para todos los pasos del
            # workflow y queda como prefijo estable de la corrida.
            step_system_prompt = _with_operation_context(
                skill.system_prompt,
                execution,
                # Bajo contexto-primero el documento principal viaja entero y
                # citable: su resumen sobra y puede contradecirlo.
                include_blueprint_summary=not use_context_first,
            )
            system_prompt = (
                build_table_system_prompt(step_system_prompt, step_table_schema)
                if is_table_step
                else step_system_prompt
            )

            tier_used = resolve_tier(skill, step)
            corpus = None
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
                corpus = build_step_corpus(
                    execution=execution,
                    step_documents=step_documents,
                    query_text=query_text,
                    reserved_tokens=reserved,
                    blueprint_id=blueprint_id,
                    document_texts=document_texts,
                    strategy=effective_retrieval_strategy,
                )
                chunks = corpus.chunks
                plan = corpus.plan
                seen_chunk_ids.update(c.id for c in chunks)
                all_step_chunks.extend(chunks)
                step_diagnostics = plan.diagnostics()

            messages = context_budget.build_messages(
                system_prompt=system_prompt,
                inventory=corpus.inventory if corpus else "",
                documents=corpus.documents if corpus else [],
                corpus_volatile=corpus.volatile if corpus else "",
                step_prompt=prompt,
                model=_resolve_model(skill, tier_used),
            )

            tool_ctx = SkillToolContext(user=execution.owner, allowed_documents=step_documents)
            # Las citas nativas vuelven por un parámetro de salida: el resto de
            # los llamadores de `generate_chat_completion` espera una tupla de
            # dos y no tiene por qué enterarse de esto.
            raw_citations: list[dict] = []
            content, usage, model_used = _call_model(
                messages,
                skill=skill,
                tier=tier_used,
                tool_ctx=tool_ctx,
                citations_out=raw_citations,
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
                strict = is_strict_step(step)
                try:
                    table, content, retry_usage = _coerce_with_retry(
                        content=content,
                        table_schema=step_table_schema,
                        strict=strict,
                        messages=messages,
                        skill=skill,
                        tier=tier_used,
                        tool_ctx=tool_ctx,
                        step=step,
                        execution=execution,
                        citations_out=raw_citations,
                    )
                except ValueError as exc:
                    if strict:
                        # En estricto no se degrada a texto: un paso que declara
                        # una determinación auditable y no puede producirla tiene
                        # que decirlo. Escribir prosa acá es lo que hacía que la
                        # corrida terminara "bien" con una determinación que
                        # nadie validó. Los pasos anteriores ya están
                        # persistidos, así que no se pierde el trabajo hecho.
                        raise
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
                    for key in total_usage:
                        total_usage[key] += retry_usage.get(key, 0)
                    step_entry["table"] = table
                    step_entry["output_validation"] = (
                        OutputValidation.STRICT if strict else OutputValidation.LENIENT
                    )
                    if table.get("issues"):
                        step_entry["table_issues"] = table["issues"]
                    step_entry["content"] = ""
                    previous_sections.append(
                        (step.title, _table_summary_for_history(step.title, table))
                    )
            else:
                step_entry["content"] = content
                previous_sections.append((step.title, content))

            # Las citas se resuelven acá y no antes de la coerción: un paso
            # tabular que reintenta descarta su primera respuesta, y con ella
            # sus citas. Resolverlas arriba dejaba el paso con las filas del
            # reintento y las fuentes de la respuesta que se tiró.
            step_citations: list[dict] = []
            if corpus is not None and raw_citations:
                step_citations = resolve_citations(raw_citations, corpus.documents)
                step_entry["retrieval"] = {
                    **step_entry.get("retrieval", {}),
                    **citation_stats(step_citations),
                }
            step_entry["citations"] = step_citations

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
    # Las citas de toda la corrida, no sólo del último tramo: es la métrica que
    # responde "¿cuánto de este informe se puede ir a verificar?".
    all_citations = [c for entry in step_results for c in entry.get("citations", [])]
    source_stats.update(citation_stats(all_citations))
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

        # La corrida queda clavada a una versión de la definición. Se resuelve
        # siempre —incluso al reanudar— porque comparar la versión actual con la
        # que quedó fijada es justamente cómo se detecta que alguien editó el
        # workflow en el medio.
        current_version = definition_module.resolve_definition_version(
            execution.skill, steps=steps
        )
        definition_changed = False
        if execution.definition_version_id is None:
            execution.definition_version = current_version
        elif execution.definition_version_id != current_version.id:
            definition_changed = True

        manifest = build_run_manifest(execution, steps)
        existing = metadata.get("run_manifest") or {}
        if existing:
            if definition_changed:
                existing["definition_changed_during_run"] = True
                existing["definition_version_now"] = current_version.version_number
                metadata["run_manifest"] = existing
        else:
            metadata["run_manifest"] = manifest
        execution.metadata = metadata

        execution.save(update_fields=[
            "status", "started_at", "document_snapshot", "error_message",
            "metadata", "definition_version",
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
# Volver a correr
# ---------------------------------------------------------------------------

# Claves de `metadata` que forman parte del input de la corrida y por lo tanto
# tienen que viajar a la repetición. El resto —usage, manifiesto, diagnósticos—
# es resultado: copiarlo sería falsificar el registro de la corrida nueva.
RERUN_METADATA_KEYS = (
    "table_columns",
    "table_schema",
    "document_slugs_filter",
    "step_document_overrides",
    "review_each_step",
)


def rerun_execution(
    execution: SkillExecution,
    *,
    owner=None,
    review_each_step: bool | None = None,
) -> SkillExecution:
    """
    Repetir una corrida con el mismo input, en una ejecución nueva.

    Es la herramienta que hacía falta para poder afirmar algo sobre el
    determinismo: reproduce a mano el alcance documental, los parámetros y las
    instrucciones extra de la corrida original, de modo que si las dos salidas
    difieren la explicación no puede ser el input.

    **No** copia la versión de definición: la repetición corre la definición de
    hoy y queda apuntando a la versión que le toque. Si alguien editó el workflow
    en el medio, la comparación lo va a decir — que es precisamente lo que se
    quiere ver, en vez de una repetición que finge ser idéntica.

    Tampoco copia la salida ni la edición humana: la corrida nueva empieza en
    blanco.
    """
    source_metadata = execution.metadata or {}
    metadata = {
        key: source_metadata[key]
        for key in RERUN_METADATA_KEYS
        if source_metadata.get(key) is not None
    }
    if review_each_step is not None:
        metadata["review_each_step"] = bool(review_each_step)
    metadata["rerun_of"] = execution.id

    return SkillExecution.objects.create(
        skill=execution.skill,
        owner=owner or execution.owner,
        repository=execution.repository,
        project=execution.project,
        document=execution.document,
        extra_instructions=execution.extra_instructions,
        input_values=dict(execution.input_values or {}),
        output_mode=execution.output_mode,
        metadata=metadata,
        status=ExecutionStatus.PENDING,
    )


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
