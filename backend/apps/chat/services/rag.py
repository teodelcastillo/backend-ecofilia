"""
RAG entry points for the chat layer.

This module hosts:
- ``fetch_relevant_chunks``: legacy vector-first retriever, kept for backward
  compatibility with existing call sites and tests.
- ``retrieve_for_chat``: the new pipeline orchestrator (hybrid retrieval, RRF,
  optional LLM reranker, diversity selection, citation-ready context block).
- ``build_context_block``: proxy to ``context_builder.build_context_block`` so
  existing imports keep working.

Design notes:
- ``retrieve_for_chat`` calls ``fetch_relevant_chunks`` inside this module for
  the vector branch. Tests can keep patching ``apps.chat.services.rag.fetch_relevant_chunks``
  and the patch is honored at call time.
- All optional steps (LLM reranker, MMR, query expansion) are gated by env flags
  so the pipeline degrades gracefully when external services are off.
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, List

from django.db.models import Q, QuerySet
from pgvector.django import CosineDistance

from apps.chat.services.context_builder import (
    build_citation_prompt,
    build_context_block as _build_context_block,
    is_mmr_enabled,
    mmr_select,
)
from apps.chat.services.query_analysis import (
    COVERAGE_MODE_ALL,
    COVERAGE_MODE_BALANCED,
    QUERY_TYPE_COMPARATIVE,
    QUERY_TYPE_EXTRACTION_PER_ENTITY,
    QUERY_TYPE_NUMERIC,
    QUERY_TYPE_PANORAMA,
    QueryAnalysis,
    apply_response_mode_override,
    build_query_set,
    build_retrieval_plan,
    classify_query,
    classify_query_hybrid,
)
from apps.chat.services.reranker import is_reranker_enabled, llm_rerank
from apps.chat.services.retrieval import cap_per_document, lexical_search, rrf_fuse
from apps.document.models import Document, SmartChunk
from apps.document.utils.client_openia import embed_text

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: str = "False") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


MAX_CONTEXT_CHUNKS = int(os.environ.get("CHAT_CONTEXT_CHUNKS", "8"))
RAG_RERANK_POOL = int(os.environ.get("RAG_RERANK_POOL", "20"))
RAG_VECTOR_POOL_MULTIPLIER = float(os.environ.get("RAG_VECTOR_POOL_MULTIPLIER", "2.5"))
RAG_LEXICAL_POOL_MULTIPLIER = float(os.environ.get("RAG_LEXICAL_POOL_MULTIPLIER", "2.0"))
RAG_MIN_SIMILARITY = float(os.environ.get("RAG_MIN_SIMILARITY", "0.3"))
RAG_ALL_DOCS_MIN_COVERAGE_RATIO = float(
    os.environ.get("RAG_ALL_DOCS_MIN_COVERAGE_RATIO", "1.0")
)

# --- Phase 1: recall-oriented retrieval ---------------------------------------
# All default ON. Set the corresponding flag to 0 to reproduce the pre-Phase-1
# behaviour for A/B measurement against the eval baseline.
#
# RAG_RECALL_MODE      : the similarity threshold stops *dropping* evidence and
#                        is used only to label confidence; the budget-capped
#                        ranked set is kept (the data, if retrieved, survives).
# RAG_PER_DOC_FLOOR /  : adaptive budget — scale the context window with the
# RAG_MAX_CONTEXT_CHUNKS number of in-scope documents for distributed/per-entity
#                        tasks, bounded so huge corpora don't blow the context.
# RAG_PARENT_EXPANSION/: small-to-big — widen each anchor chunk with its
# RAG_PARENT_WINDOW      neighbours so the model reads a contiguous passage.
RAG_RECALL_MODE = _env_bool("RAG_RECALL_MODE", "True")
RAG_PER_DOC_FLOOR = int(os.environ.get("RAG_PER_DOC_FLOOR", "1"))
RAG_MAX_CONTEXT_CHUNKS = int(os.environ.get("RAG_MAX_CONTEXT_CHUNKS", "24"))
RAG_PARENT_EXPANSION = _env_bool("RAG_PARENT_EXPANSION", "True")
RAG_PARENT_WINDOW = int(os.environ.get("RAG_PARENT_WINDOW", "1"))


GENERAL_QUERY_PATTERNS = (
    r"\b(resumen|panorama|vision|visión)\b",
    r"\b(general|global|integral|completo)\b",
    r"\b(todo|toda|todos|todas)\b",
    r"\b(base documental|documentacion|documentación)\b",
    r"\b(overall|high[- ]level|across)\b",
)


def is_general_query(query_text: str) -> bool:
    """Backward-compatible heuristic kept for legacy callers."""
    text = (query_text or "").strip().lower()
    if not text:
        return False
    if len(text.split()) >= 18:
        return True
    return any(re.search(pattern, text) for pattern in GENERAL_QUERY_PATTERNS)


# ---------------------------------------------------------------------------
# Public dataclass used by callers that want full pipeline diagnostics
# ---------------------------------------------------------------------------


@dataclass
class RetrievalResult:
    chunks: List[SmartChunk] = field(default_factory=list)
    context_block: str = ""
    analysis: QueryAnalysis | None = None
    diagnostics: dict = field(default_factory=dict)
    recommended_documents: list[dict] = field(default_factory=list)

    @property
    def chunk_ids(self) -> List[int]:
        return [c.id for c in self.chunks]

    @property
    def covered_document_ids(self) -> set:
        return {c.document_id for c in self.chunks}


def retrieve_from_library(
    *,
    user,
    query_text: str,
    top_n: int | None = None,
) -> "RetrievalResult":
    """
    Library-wide hybrid retrieval for general chat (no explicit document scope).

    Design principle: constant memory cost, scales to millions of chunks.

    Pipeline:
    1. Fast retrieval gate — skip for greetings/trivial queries (no DB hit).
    2. Pre-compute accessible document IDs in one SQL query on the documents
       table (cheap: just IDs, no chunk data loaded).
    3. Single pgvector vector search: WHERE document_id IN (...) ORDER BY
       embedding <=> $vec LIMIT top_n — pgvector returns only top_n rows;
       Python never loads the embeddings of chunks that didn't rank.
    4. Single trigram lexical search with the same scope.
    5. RRF fusion of vector + lexical lists.
    6. Relevance threshold filter.
    7. Citation-ready context block.

    Memory usage: O(top_n) regardless of library size.
    With an HNSW index the vector step is O(log N) in the DB.
    """
    from apps.document.models import ChunkingStatus
    from apps.document.services import accessible_library_documents

    top_n = top_n or int(os.environ.get("LIBRARY_RETRIEVAL_TOP_N", "15"))
    started = time.perf_counter()

    diagnostics: dict = {
        "retrieval_mode": "library_global",
        "retrieval_skipped_reason": None,
        "documents_in_scope": 0,
        "final_chunks": 0,
        "elapsed_seconds": 0.0,
    }

    if not query_text:
        return RetrievalResult(diagnostics=diagnostics)

    # ── 1. Retrieval gate: skip expensive DB work for trivial queries ──────
    analysis = classify_query(query_text)  # pure-regex, no LLM call
    retrieval_mode, skip_reason = _decide_retrieval_mode(query_text, analysis, doc_count=0)
    diagnostics["retrieval_skipped_reason"] = skip_reason
    if retrieval_mode == "none":
        diagnostics["elapsed_seconds"] = round(time.perf_counter() - started, 4)
        return RetrievalResult(analysis=analysis, diagnostics=diagnostics)

    # ── PANORAMA/COMPARATIVE: expand pool + per-doc cap for broad queries ──
    # When the query asks for information across many entities (countries,
    # sectors, years), all relevant documents sit at very similar cosine distances
    # so a small pool is dominated by whichever 2-3 docs rank first.
    #
    # Strategy:
    # - Expand pool by LIBRARY_PANORAMA_POOL_MULTIPLIER (default 2) — kept ≤ 2×
    #   so we stay within pgvector's default hnsw.ef_search=40; larger values
    #   degrade HNSW recall and add latency without benefit.
    # - Per-document cap of 1 chunk maximises unique-document coverage.
    # - Skip the RAG_MIN_SIMILARITY threshold for broad queries: every document
    #   in the library is a plausible source for a pan-regional listing query,
    #   and the threshold would kill the diversity gain from the wider pool.
    is_broad_query = analysis.query_type in {QUERY_TYPE_PANORAMA, QUERY_TYPE_COMPARATIVE}
    panorama_multiplier = int(os.environ.get("LIBRARY_PANORAMA_POOL_MULTIPLIER", "2"))
    pool_top_n = (top_n * panorama_multiplier) if is_broad_query else top_n
    # Final result: up to 1 chunk/doc, max LIBRARY_PANORAMA_FINAL (default 25).
    final_limit = int(os.environ.get("LIBRARY_PANORAMA_FINAL", "25")) if is_broad_query else top_n
    max_per_doc = 1 if is_broad_query else None

    # ── 2. Accessible document IDs — query on documents table only ─────────
    # .values_list().distinct() on Document (small table) is fast.
    # Hard cap at 2000 docs to keep the IN clause sane; real libraries will
    # be much smaller for a long time.
    accessible_doc_ids: list[int] = list(
        accessible_library_documents(user)
        .filter(chunking_status=ChunkingStatus.DONE)
        .values_list("id", flat=True)
        .distinct()[:2000]
    )
    diagnostics["documents_in_scope"] = len(accessible_doc_ids)
    if not accessible_doc_ids:
        diagnostics["elapsed_seconds"] = round(time.perf_counter() - started, 4)
        return RetrievalResult(analysis=analysis, diagnostics=diagnostics)

    # ── 2b. Geographic scope filter (broad queries only) ──────────────────
    # Detects a regional scope in the query ("Sudamérica", "LATAM", etc.) and
    # filters accessible_doc_ids to only include documents whose region or name
    # matches that scope.  Falls back to the full scope when fewer than
    # _GEO_FILTER_MIN_DOCS documents survive the filter (avoids empty results
    # caused by missing Document.region metadata).
    geo_scope: str | None = None
    diagnostics["geo_scope"] = None
    diagnostics["geo_filter_applied"] = False
    diagnostics["geo_filtered_docs"] = None
    if is_broad_query:
        geo_scope = _extract_geo_scope(query_text)
        diagnostics["geo_scope"] = geo_scope
        if geo_scope:
            geo_doc_ids = _filter_docs_by_geo(accessible_doc_ids, geo_scope)
            diagnostics["geo_filtered_docs"] = len(geo_doc_ids)
            if len(geo_doc_ids) >= _GEO_FILTER_MIN_DOCS:
                accessible_doc_ids = geo_doc_ids
                diagnostics["geo_filter_applied"] = True
                logger.info(
                    "Geo filter applied: scope=%s, docs_before=%d, docs_after=%d",
                    geo_scope,
                    diagnostics["documents_in_scope"],
                    len(accessible_doc_ids),
                )
            else:
                logger.info(
                    "Geo filter skipped (too few docs): scope=%s, matched=%d",
                    geo_scope,
                    len(geo_doc_ids),
                )

    # ── 2c. Re-scale pool when geo-filter shrinks the scope ───────────────
    # pool_top_n was sized for the full library (top_n × multiplier = 30 by
    # default).  After geo-filtering the accessible pool drops to ~10-50 docs.
    # If pool_top_n < doc_count, some documents never get a chunk into the
    # pgvector LIMIT window, so they can't appear in the final result no
    # matter how we cap or rerank afterwards.
    #
    # Example: 35 geo-filtered docs, pool_top_n=30 → the 5 docs whose best
    # chunk ranks ≥31 by cosine distance are permanently excluded.  A
    # Portuguese-language NDC (Brasil) sitting at position 31 will never
    # appear even though it's in the geo-filtered scope.
    #
    # Fix: ensure every geo-filtered doc has GEO_POOL_SLOTS_PER_DOC (default 3)
    # chunk slots in the candidate pool.  Also relax final_limit so the
    # per-doc cap doesn't immediately undo the coverage gain.
    #
    # Note: for filtered pgvector queries on small sets (<200 docs) the planner
    # often switches to a sequential scan, so a larger LIMIT is cheap; for
    # larger sets the HNSW ef_search budget still applies but the filter
    # already narrows the graph traversal.
    if diagnostics["geo_filter_applied"]:
        _geo_n = len(accessible_doc_ids)
        _slots = int(os.environ.get("GEO_POOL_SLOTS_PER_DOC", "3"))
        pool_top_n = max(pool_top_n, _geo_n * _slots)
        # Allow up to 1 chunk per geo-filtered doc in the final result,
        # capped at GEO_PANORAMA_FINAL_CAP to keep LLM context manageable.
        _geo_final_cap = int(os.environ.get("GEO_PANORAMA_FINAL_CAP", "40"))
        final_limit = max(final_limit, min(_geo_n, _geo_final_cap))
        logger.debug(
            "Geo pool rescaled: geo_docs=%d pool_top_n=%d final_limit=%d",
            _geo_n, pool_top_n, final_limit,
        )

    # ── 3. Vector search: ONE query, pgvector does the heavy lifting ───────
    # No JOINs, no DISTINCT on the chunks table (incompatible with ORDER BY
    # embedding), no Python-side embedding comparisons.
    query_embedding = embed_text(query_text)
    vector_chunks: list[SmartChunk] = []
    if query_embedding:
        vector_chunks = list(
            SmartChunk.objects.filter(
                document_id__in=accessible_doc_ids,
                embedding__isnull=False,
            )
            .annotate(distance=CosineDistance("embedding", query_embedding))
            .order_by("distance")[:pool_top_n]
        )

    # ── 4. Lexical search: trigram similarity, same scope ──────────────────
    base_qs = SmartChunk.objects.filter(
        document_id__in=accessible_doc_ids,
        embedding__isnull=False,
    )
    lex_chunks: list[SmartChunk] = []
    try:
        lex_chunks = lexical_search(base_qs, query_text, top_n=pool_top_n)
    except Exception as exc:
        logger.warning("Library lexical search failed: %s", exc)

    # ── 5. RRF fusion ──────────────────────────────────────────────────────
    ranked_lists: list[list[SmartChunk]] = []
    if vector_chunks:
        ranked_lists.append(vector_chunks)
    if lex_chunks:
        ranked_lists.append(lex_chunks)

    if not ranked_lists:
        diagnostics["elapsed_seconds"] = round(time.perf_counter() - started, 4)
        return RetrievalResult(analysis=analysis, diagnostics=diagnostics)

    fused = rrf_fuse(ranked_lists, top_n=pool_top_n)

    # ── 5b. Per-document cap for PANORAMA/COMPARATIVE diversity ───────────
    # Applied after RRF so ranking order is preserved; cap just prevents any
    # single document from consuming more than max_per_doc slots.
    if max_per_doc is not None:
        fused = cap_per_document(fused, max_per_doc=max_per_doc, total_limit=final_limit)

    # ── 6. Relevance threshold ─────────────────────────────────────────────
    # For broad queries the threshold is intentionally skipped: all documents in
    # the library are plausible sources and the cap already bounds the output.
    if is_broad_query:
        final_chunks = list(fused)
    else:
        final_chunks = [
            c for c in fused
            if _chunk_similarity(c) is None or _chunk_similarity(c) >= RAG_MIN_SIMILARITY
        ]
        final_chunks = final_chunks[:top_n]

    # ── 7. Context block ───────────────────────────────────────────────────
    context_block = _build_context_block(final_chunks, with_citations=True)

    diagnostics["final_chunks"] = len(final_chunks)
    diagnostics["unique_documents"] = len({c.document_id for c in final_chunks})
    diagnostics["elapsed_seconds"] = round(time.perf_counter() - started, 4)

    return RetrievalResult(
        chunks=final_chunks,
        context_block=context_block,
        analysis=analysis,
        diagnostics=diagnostics,
    )


def suggest_related_library_documents(
    *,
    user,
    query_text: str,
    exclude_document_ids: Iterable[int],
    top_n: int | None = None,
    chunk_pool: int | None = None,
    doc_pool: int | None = None,
) -> list[dict]:
    """
    Busca en toda la biblioteca accesible fragmentos similares a la pregunta,
    excluyendo documentos ya vinculados a la sesión/proyecto. Devuelve filas
    ``{id, slug, name, relevance_score}`` para mostrar como "documentos relacionados".
    """
    from apps.document.services import accessible_library_documents

    text = (query_text or "").strip()
    if not text:
        return []

    exclude = {int(x) for x in exclude_document_ids}
    top_n = top_n or int(os.environ.get("CHAT_LIBRARY_RECOMMEND_TOP_DOCS", "5"))
    chunk_pool = chunk_pool or int(os.environ.get("CHAT_LIBRARY_RECOMMEND_CHUNK_POOL", "56"))
    doc_pool = doc_pool or int(os.environ.get("CHAT_LIBRARY_RECOMMEND_DOC_POOL", "500"))

    lib = accessible_library_documents(user).exclude(id__in=exclude)
    lib = lib.filter(chunks__embedding__isnull=False).distinct()
    candidate_ids = list(lib.values_list("id", flat=True)[:doc_pool])
    if not candidate_ids:
        return []

    chunk_qs = SmartChunk.objects.filter(document_id__in=candidate_ids).exclude(
        embedding__isnull=True
    )
    if not user.is_staff:
        from apps.document.services import visible_documents_q

        chunk_qs = chunk_qs.filter(visible_documents_q(user, prefix="document__")).distinct()

    chunks = list(chunk_qs.top_similar2(text, top_n=chunk_pool))
    if not chunks:
        return []

    doc_scores: dict[int, float] = defaultdict(float)
    doc_hits: dict[int, int] = defaultdict(int)
    for chunk in chunks:
        dist = getattr(chunk, "distance", None)
        if dist is not None:
            sim = max(0.0, 1.0 - float(dist))
        else:
            sim = 0.5
        doc_scores[chunk.document_id] += sim
        doc_hits[chunk.document_id] += 1

    ranked_ids = sorted(
        doc_scores.keys(),
        key=lambda did: (doc_scores[did], doc_hits[did]),
        reverse=True,
    )[:top_n]

    rows = list(Document.objects.filter(id__in=ranked_ids).values("id", "slug", "name"))
    by_id = {r["id"]: r for r in rows}
    out: list[dict] = []
    for did in ranked_ids:
        row = by_id.get(did)
        if not row:
            continue
        raw = doc_scores[did]
        relevance_score = min(100, round(20.0 * raw + 5 * doc_hits[did]))
        out.append(
            {
                "id": row["id"],
                "slug": row["slug"],
                "name": row["name"],
                "relevance_score": max(1, relevance_score),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Legacy vector-first retriever (still used by the orchestrator and tests)
# ---------------------------------------------------------------------------


def fetch_relevant_chunks(
    *,
    user,
    query_text: str,
    allowed_documents: QuerySet[Document],
    top_n: int | None = None,
    topics: list[str] | None = None,
    retrieval_strategy: str = "global",
    k_per_doc: int = 2,
    total_limit: int | None = None,
    max_chunks_per_doc: int | None = None,
) -> List[SmartChunk]:
    """
    Returns the most relevant chunks limited to the allowed documents for the session.
    Strategies:
      - ``global``: classic vector-only top-N over the whole pool.
      - ``hybrid_per_document``: ensures recall coverage by taking top-k per
        document, then reranks globally with per-document caps.
      - ``auto``: picks ``hybrid_per_document`` for broad/multi-doc questions and
        ``global`` otherwise.
    """
    top_n = top_n or MAX_CONTEXT_CHUNKS
    total_limit = total_limit or top_n
    max_chunks_per_doc = max_chunks_per_doc or total_limit

    if not query_text:
        return []

    doc_ids = list(allowed_documents.values_list("id", flat=True))
    if not doc_ids:
        return []

    # Chunks sin embedding rompen CosineDistance en pgvector y provocan 500
    qs = SmartChunk.objects.filter(document_id__in=doc_ids).exclude(embedding__isnull=True)
    if not user.is_staff:
        from apps.document.services import visible_documents_q

        qs = qs.filter(visible_documents_q(user, prefix="document__")).distinct()

    if topics:
        topic_filter = Q()
        for topic in topics:
            if topic:
                topic_filter |= Q(content__icontains=topic)
        if topic_filter:
            filtered_by_topics = qs.filter(topic_filter)
            if filtered_by_topics.exists():
                qs = filtered_by_topics

    if not qs.exists():
        return []

    resolved_strategy = retrieval_strategy
    if retrieval_strategy == "auto":
        if is_general_query(query_text):
            resolved_strategy = "hybrid_per_document"
        else:
            resolved_strategy = "global"

    if resolved_strategy != "hybrid_per_document":
        return list(qs.top_similar2(query_text, top_n=top_n))

    query_embedding = embed_text(query_text)
    if not query_embedding:
        return list(qs.top_similar2(query_text, top_n=top_n))

    ranked_qs = qs.annotate(
        distance=CosineDistance("embedding", query_embedding)
    ).order_by("distance")

    # Phase 1: per-document recall coverage.
    candidates: list[SmartChunk] = []
    safe_k_per_doc = max(1, k_per_doc)
    for doc_id in doc_ids:
        doc_top = list(ranked_qs.filter(document_id=doc_id)[:safe_k_per_doc])
        candidates.extend(doc_top)

    if not candidates:
        return []

    by_id: dict[int, SmartChunk] = {}
    for chunk in candidates:
        existing = by_id.get(chunk.id)
        if existing is None:
            by_id[chunk.id] = chunk
            continue
        if getattr(chunk, "distance", 1.0) < getattr(existing, "distance", 1.0):
            by_id[chunk.id] = chunk

    # Phase 2: global rerank with per-document cap and total limit.
    merged = sorted(
        by_id.values(),
        key=lambda c: getattr(c, "distance", 1.0),
    )

    return cap_per_document(
        merged,
        max_per_doc=max_chunks_per_doc,
        total_limit=total_limit,
    )


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


def _scope_qs_for_user(user, doc_ids: list) -> QuerySet[SmartChunk]:
    qs = SmartChunk.objects.filter(document_id__in=doc_ids).exclude(embedding__isnull=True)
    if user is not None and not getattr(user, "is_staff", False):
        from apps.document.services import visible_documents_q

        qs = qs.filter(visible_documents_q(user, prefix="document__")).distinct()
    return qs


def _single_doc_retrieval_fallback(
    user,
    doc_ids: list[int],
    query_text: str,
    *,
    total_limit: int,
) -> list[SmartChunk]:
    """
    When vector/lexical fusion yields nothing for a single-document session,
    pull the best available chunks (including chunk 0 summary) without the
    similarity threshold so short queries like "Resúmelo" still get context.
    """
    if len(doc_ids) != 1:
        return []
    base_qs = _scope_qs_for_user(user, doc_ids)
    if not base_qs.exists():
        return []
    filled = _fill_missing_documents(
        base_qs=base_qs,
        query_text=query_text,
        missing_doc_ids=doc_ids,
        existing_chunks=[],
    )
    return filled[:total_limit]


def _fill_missing_documents(
    *,
    base_qs: QuerySet[SmartChunk],
    query_text: str,
    missing_doc_ids: list,
    existing_chunks: list[SmartChunk],
) -> list[SmartChunk]:
    """
    Coverage fallback: retrieve one best chunk for every document missing from
    the final context. This is intentionally simple and deterministic; for
    all-docs prompts, predictability is more important than pure top-k score.
    """
    if not missing_doc_ids:
        return existing_chunks

    try:
        query_embedding = embed_text(query_text)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Coverage fallback embedding failed: %s", exc)
        query_embedding = None

    filled = list(existing_chunks)
    for doc_id in missing_doc_ids:
        doc_qs = base_qs.filter(document_id=doc_id)
        if query_embedding:
            chunk = (
                doc_qs.annotate(distance=CosineDistance("embedding", query_embedding))
                .order_by("distance", "chunk_index")
                .first()
            )
        else:
            chunk = doc_qs.order_by("chunk_index").first()
        if chunk is not None:
            filled.append(chunk)
    return filled


def _expand_to_parents(
    chunks: list[SmartChunk],
    user,
    *,
    window: int,
    max_total: int,
) -> list[SmartChunk]:
    """Small-to-big expansion.

    Widen each anchor chunk with its neighbours from the same document
    (``chunk_index`` within ±``window``), reconstructing a contiguous passage so
    the model reads surrounding context instead of an isolated fragment. Anchor
    chunks are always retained; only neighbours are trimmed to honour
    ``max_total``. Output is grouped by document (anchor order) and ordered by
    ``chunk_index`` so the context block reads contiguously.
    """
    if window <= 0 or not chunks:
        return list(chunks)

    anchors_by_doc: dict[int, set[int]] = {}
    doc_order: list[int] = []
    for c in chunks:
        if c.document_id not in anchors_by_doc:
            anchors_by_doc[c.document_id] = set()
            doc_order.append(c.document_id)
        anchors_by_doc[c.document_id].add(c.chunk_index)

    # Desired indices (anchors + neighbours) per document.
    want: dict[int, set[int]] = {}
    for doc_id, idxs in anchors_by_doc.items():
        wanted: set[int] = set()
        for i in idxs:
            for j in range(i - window, i + window + 1):
                if j >= 0:
                    wanted.add(j)
        want[doc_id] = wanted

    by_key: dict[tuple[int, int], SmartChunk] = {
        (c.document_id, c.chunk_index): c for c in chunks
    }
    base = _scope_qs_for_user(user, list(anchors_by_doc.keys()))
    for doc_id, idxs in want.items():
        missing = sorted(
            i for i in idxs if (doc_id, i) not in by_key
        )
        if not missing:
            continue
        for neighbour in base.filter(document_id=doc_id, chunk_index__in=missing):
            by_key[(neighbour.document_id, neighbour.chunk_index)] = neighbour

    # Ordered output: (doc_id, chunk_index, is_anchor).
    ordered: list[tuple[int, int, bool]] = []
    for doc_id in doc_order:
        for i in sorted(want[doc_id]):
            if (doc_id, i) in by_key:
                ordered.append((doc_id, i, i in anchors_by_doc[doc_id]))

    # Trim neighbours (never anchors) to honour the budget.
    if len(ordered) > max_total:
        anchors = [t for t in ordered if t[2]]
        neighbours = [t for t in ordered if not t[2]]
        keep_neighbours = max(0, max_total - len(anchors))
        keep = {(d, i) for (d, i, _) in anchors}
        keep |= {(d, i) for (d, i, _) in neighbours[:keep_neighbours]}
        ordered = [t for t in ordered if (t[0], t[1]) in keep]

    return [by_key[(d, i)] for (d, i, _) in ordered]


def _chunk_similarity(chunk: SmartChunk) -> float | None:
    """Best-effort normalized similarity in [0, 1] for relevance filtering."""
    distance = getattr(chunk, "distance", None)
    if distance is not None:
        try:
            return max(0.0, min(1.0, 1.0 - float(distance)))
        except (TypeError, ValueError):
            return None
    lex_sim = getattr(chunk, "lex_sim", None)
    if lex_sim is not None:
        try:
            return max(0.0, min(1.0, float(lex_sim)))
        except (TypeError, ValueError):
            return None
    return None


_RETRIEVAL_DOMAIN_HINTS = (
    "ndc",
    "nap",
    "acuerdo de parís",
    "paris agreement",
    "mitigación",
    "mitigacion",
    "adaptación",
    "adaptacion",
    "emisiones",
    "clima",
    "climático",
    "climatico",
)

# ---------------------------------------------------------------------------
# Geographic scope filtering (PANORAMA / COMPARATIVE queries)
# ---------------------------------------------------------------------------
# Scopes are ordered from most-specific to least-specific.
# _extract_geo_scope() returns the FIRST matching scope, so sub-regional
# patterns (mercosur, andina) must appear before their parent (sudamerica),
# and continental groupings before latam.
#
# Pattern rules:
# - Use \b word boundaries to avoid partial matches.
# - Include Spanish, English and common abbreviation variants.
# - Avoid ambiguous bare words: "pacific" can mean "Acuerdo del Pacífico"
#   (a trade bloc), so oceania requires "islas del Pacífico" or "Oceanía".
_GEO_SCOPE_PATTERNS: dict[str, str] = {
    # ── Sub-regional LATAM (most specific — checked before sudamerica/latam) ─
    "mercosur": (
        r"\b(mercosur|pa[ií]ses? del mercosur|bloque del mercosur)\b"
    ),
    "andina": (
        r"\b(comunidad andina|regi[oó]n andina|pa[ií]ses? andinos?|andean "
        r"community|andean countries)\b"
    ),
    # ── Broad LATAM sub-regions ───────────────────────────────────────────────
    "sudamerica": (
        r"\b(sudamérica|sudamerica|sudamericanos?|am[eé]rica del sur|south america"
        r"|pa[ií]ses? sudamericanos?|cono sur)\b"
    ),
    "centroamerica": (
        r"\b(centroam[eé]rica|am[eé]rica central|central america"
        r"|pa[ií]ses? centroamericanos?)\b"
    ),
    "caribe": (
        r"\b(carib[eé]|caribbean|pa[ií]ses? caribeños?|islas? caribeñas?)\b"
    ),
    "norteamerica": (
        r"\b(am[eé]rica del norte|norteam[eé]rica|north america"
        r"|pa[ií]ses? norteamericanos?)\b"
    ),
    "latam": (
        r"\b(am[eé]rica latina|latinoam[eé]rica|latam|latin america"
        r"|pa[ií]ses? latinoamericanos?|iberoam[eé]rica|iberoamerica)\b"
    ),
    # ── Rest of world ─────────────────────────────────────────────────────────
    "europa": (
        r"\b(europa|europe|europeos?|european|uni[oó]n europea|european union"
        r"|pa[ií]ses? europeos?)\b"
    ),
    "oriente_medio": (
        r"\b(oriente medio|middle east|pa[ií]ses? [aá]rabes?|regi[oó]n [aá]rabe"
        r"|golfo p[eé]rsico|persian gulf)\b"
    ),
    "africa": (
        r"\b([aá]frica|africa|africanos?|african|pa[ií]ses? africanos?"
        r"|sub[- ]?sahariano?s?)\b"
    ),
    "asia": (
        r"\b(asia(?!\s*pac[ií]fico)|asi[aá]tico|asian|pa[ií]ses? asi[aá]ticos?"
        r"|sureste asi[aá]tico|southeast asia)\b"
    ),
    "oceania": (
        r"\b(ocean[ií]a|oceania|islas? del pac[ií]fico|pacific islands?"
        r"|pa[ií]ses? del pac[ií]fico|small island developing)\b"
    ),
}

# Country / region name substrings matched against Document.region and Document.name
# using case-insensitive containment (icontains).  Each list covers the canonical
# country names that belong to that scope so a document tagged "Argentina" is
# found when the query scope is "sudamerica", "mercosur", or "latam".
_GEO_COUNTRY_LISTS: dict[str, list[str]] = {
    "mercosur": [
        "argentina", "brasil", "brazil", "uruguay", "paraguay", "bolivia",
        "venezuela", "mercosur",
    ],
    "andina": [
        "bolivia", "colombia", "ecuador", "perú", "peru",
        "comunidad andina",
    ],
    "sudamerica": [
        "argentina", "bolivia", "brasil", "brazil", "chile", "colombia",
        "ecuador", "guyana", "paraguay", "perú", "peru", "surinam", "suriname",
        "uruguay", "venezuela",
        "sudamérica", "sudamerica", "sur américa", "sur america",
        "south america", "américa del sur", "america del sur",
    ],
    "centroamerica": [
        "costa rica", "el salvador", "guatemala", "honduras", "nicaragua",
        "panamá", "panama", "belice", "belize",
        "centroamérica", "centroamerica", "central america",
    ],
    "caribe": [
        "cuba", "república dominicana", "dominicana", "haití", "haiti",
        "jamaica", "trinidad", "barbados", "bahamas", "antigua", "dominica",
        "granada", "grenada", "san vicente", "saint vincent",
        "caribe", "caribbean",
    ],
    "norteamerica": [
        "méxico", "mexico", "estados unidos", "united states", "canadá", "canada",
        "norteamérica", "norteamerica", "north america",
    ],
    "latam": [
        "argentina", "bolivia", "brasil", "brazil", "chile", "colombia",
        "costa rica", "cuba", "ecuador", "el salvador", "guatemala", "honduras",
        "méxico", "mexico", "nicaragua", "panamá", "panama", "paraguay",
        "perú", "peru", "dominicana", "surinam", "suriname", "uruguay",
        "venezuela", "belice", "belize", "haití", "haiti",
        "latinoam", "latam", "américa latina", "america latina",
    ],
    "europa": [
        "alemania", "germany", "austria", "bélgica", "belgica", "belgium",
        "bulgaria", "chipre", "cyprus", "croacia", "croatia", "dinamarca", "denmark",
        "eslovenia", "slovenia", "eslovaquia", "slovakia", "españa", "spain",
        "estonia", "finlandia", "finland", "francia", "france",
        "grecia", "greece", "hungría", "hungary", "irlanda", "ireland",
        "italia", "italy", "letonia", "latvia", "lituania", "lithuania",
        "luxemburgo", "luxembourg", "malta", "países bajos", "netherlands",
        "holanda", "holland", "polonia", "poland", "portugal",
        "república checa", "czech", "rumanía", "romania", "suecia", "sweden",
        "noruega", "norway", "suiza", "switzerland", "reino unido", "united kingdom",
        "islandia", "iceland", "albania", "serbia", "montenegro", "moldova",
        "ucrania", "ukraine", "georgia", "armenia", "azerbaiyán", "azerbaijan",
        "europa", "europe",
    ],
    "oriente_medio": [
        "arabia saudita", "saudi", "emiratos", "qatar", "kuwait",
        "baréin", "bahrain", "omán", "oman", "irak", "iraq", "irán", "iran",
        "siria", "syria", "líbano", "lebanon", "jordania", "jordan",
        "israel", "palestina", "palestine", "yemen", "turquía", "turkey",
        "oriente medio", "middle east",
    ],
    "africa": [
        "nigeria", "kenia", "kenya", "etiopía", "etiopia", "ethiopia",
        "sudáfrica", "south africa", "marruecos", "morocco", "egipto", "egypt",
        "ghana", "tanzania", "uganda", "ruanda", "rwanda", "mozambique",
        "angola", "zambia", "zimbabue", "zimbabwe", "malí", "mali",
        "senegal", "camerún", "cameroon", "costa de marfil", "ivory coast",
        "madagascar", "malawi", "namibia", "botswana", "mauritania",
        "gambia", "guinea", "benin", "togo", "eritrea", "somalia",
        "sudán", "sudan", "libia", "libya", "argelia", "algeria",
        "túnez", "tunisia", "chad", "níger", "niger", "burkina faso",
        "liberia", "sierra leona",
        "africa", "áfrica",
    ],
    "asia": [
        "china", "india", "indonesia", "japón", "japan", "vietnam",
        "tailandia", "thailand", "malasia", "malaysia", "filipinas", "philippines",
        "corea del sur", "south korea", "corea del norte", "north korea",
        "bangladesh", "pakistán", "pakistan", "nepal", "sri lanka",
        "myanmar", "camboya", "cambodia", "laos", "mongolia",
        "kirguistán", "kyrgyzstan", "uzbekistán", "uzbekistan",
        "tayikistán", "tajikistan", "turkmenistán", "turkmenistan",
        "kazajistán", "kazakhstan", "timor", "singapur", "singapore",
        "brunéi", "brunei", "bután", "bhutan", "maldivas", "maldives",
        "asia",
    ],
    "oceania": [
        "australia", "nueva zelanda", "new zealand", "fiyi", "fiji", "tuvalu",
        "vanuatu", "samoa", "kiribati", "micronesia", "tonga",
        "islas salomón", "solomon islands", "nauru", "marshall",
        "palaos", "palau", "papúa", "papua",
        "oceanía", "oceania",
    ],
}

# Minimum number of geo-filtered documents required to actually apply the filter.
# Below this threshold we fall back to the full unfiltered scope so the user
# doesn't get empty or degenerate results due to missing metadata.
_GEO_FILTER_MIN_DOCS: int = 3


def _extract_geo_scope(text: str) -> str | None:
    """
    Return the first matching geographic scope key for ``text``, or ``None``.

    Scopes are checked from most-specific to least-specific so that
    "países de Sudamérica" maps to ``"sudamerica"`` rather than ``"latam"``.
    """
    norm = (text or "").strip().lower()
    if not norm:
        return None
    for scope, pattern in _GEO_SCOPE_PATTERNS.items():
        if re.search(pattern, norm):
            return scope
    return None


def _filter_docs_by_geo(doc_ids: list[int], geo_scope: str) -> list[int]:
    """
    Return the subset of ``doc_ids`` whose Document.region OR Document.name
    contains at least one of the country / region strings for ``geo_scope``.

    Falls back to the full list when the scope is unrecognised or the lookup
    would be empty.
    """
    country_strings = _GEO_COUNTRY_LISTS.get(geo_scope, [])
    if not country_strings or not doc_ids:
        return doc_ids

    q = Q()
    for cs in country_strings:
        q |= Q(region__icontains=cs)
        q |= Q(name__icontains=cs)

    return list(
        Document.objects.filter(id__in=doc_ids)
        .filter(q)
        .values_list("id", flat=True)
        .distinct()
    )


def _decide_retrieval_mode(
    query_text: str,
    analysis: QueryAnalysis,
    *,
    doc_count: int = 0,
) -> tuple[str, str | None]:
    """
    Decide retrieval mode before expensive chunk search.
    Returns (mode, skip_reason) where mode is none|light|full.
    """
    if not _env_bool("RAG_RETRIEVAL_GATE_ENABLED", "True"):
        return "full", None

    norm = (query_text or "").strip().lower()
    words = norm.split()
    if not norm:
        return "none", "empty_query"

    # Single-document sessions: always retrieve (light) so short queries like
    # "Resúmelo" still pull document context instead of skipping entirely.
    if doc_count == 1:
        return "light", None

    trivial = len(words) <= 3
    greeting_like = norm in {"hola", "hello", "hi", "buenas", "q mas hay", "qué más hay", "que mas hay"}
    has_domain_hint = any(h in norm for h in _RETRIEVAL_DOMAIN_HINTS)

    if greeting_like or (trivial and not has_domain_hint):
        return "none", "simple_query"

    if analysis.coverage_mode == COVERAGE_MODE_ALL:
        return "full", None
    if analysis.query_type in {QUERY_TYPE_PANORAMA, QUERY_TYPE_COMPARATIVE}:
        return "full", None

    if _env_bool("RAG_LIGHT_MODE_ENABLED", "True"):
        return "light", None
    return "full", None


def retrieve_for_chat(
    *,
    user,
    query_text: str,
    allowed_documents: QuerySet[Document],
    top_n: int | None = None,
    total_limit: int | None = None,
    max_chunks_per_doc: int | None = None,
    k_per_doc: int = 2,
    topics: list[str] | None = None,
    response_mode: str | None = None,
    retrieval_strategy: str | None = None,
) -> RetrievalResult:
    """
    Full RAG retrieval pipeline:

    1. Classify and (optionally) decompose the query.
    2. Run vector retrieval (per sub-query) via ``fetch_relevant_chunks``.
    3. Run lexical (trigram) retrieval over the same scope.
    4. Fuse with Reciprocal Rank Fusion (RRF).
    5. Optionally LLM-rerank the top-N pool.
    6. Apply per-document cap and (optionally) MMR diversity.
    7. Build a citation-ready context block.

    Returns a ``RetrievalResult`` with chunks, context, query analysis and
    diagnostics suitable for logging/observability.
    """
    started = time.perf_counter()
    diagnostics: dict = {
        "vector_candidates": 0,
        "lexical_candidates": 0,
        "fused_candidates": 0,
        "reranked": False,
        "mmr_applied": False,
        "sub_queries": 0,
        "documents_in_scope": 0,
        "coverage_mode": "focused",
        "coverage_target_ratio": None,
        "coverage_target_documents": None,
        "coverage_missing_documents": [],
        "coverage_met": True,
        "elapsed_seconds": 0.0,
        "max_similarity": None,
        "avg_similarity": None,
        "retrieval_confidence": "none",
        "retrieval_mode": "full",
        "retrieval_timed_out": False,
        "retrieval_skipped_reason": None,
    }

    if not query_text:
        return RetrievalResult(diagnostics=diagnostics)

    doc_ids = list(allowed_documents.values_list("id", flat=True))
    diagnostics["documents_in_scope"] = len(doc_ids)
    if not doc_ids:
        diagnostics["elapsed_seconds"] = time.perf_counter() - started
        return RetrievalResult(diagnostics=diagnostics)

    analysis = classify_query_hybrid(query_text)
    analysis = apply_response_mode_override(analysis, response_mode)
    retrieval_mode, skip_reason = _decide_retrieval_mode(
        query_text, analysis, doc_count=len(doc_ids)
    )
    diagnostics["retrieval_mode"] = retrieval_mode
    diagnostics["retrieval_skipped_reason"] = skip_reason

    if retrieval_mode == "none":
        diagnostics["elapsed_seconds"] = round(time.perf_counter() - started, 4)
        return RetrievalResult(analysis=analysis, diagnostics=diagnostics)

    queries = build_query_set(analysis)
    diagnostics["query_type"] = analysis.query_type
    diagnostics["coverage_mode"] = analysis.coverage_mode
    diagnostics["response_mode_override"] = response_mode or None
    diagnostics["classifier_source"] = analysis.classifier_source
    diagnostics["classifier_confidence"] = analysis.classifier_confidence
    diagnostics["sub_queries"] = max(0, len(queries) - 1)

    requires_all_docs = (
        analysis.coverage_mode == COVERAGE_MODE_ALL
        or analysis.query_type == QUERY_TYPE_EXTRACTION_PER_ENTITY
    )
    coverage_target_ratio = RAG_ALL_DOCS_MIN_COVERAGE_RATIO if requires_all_docs else None
    diagnostics["coverage_target_ratio"] = coverage_target_ratio

    retrieval_budget_ms = int(os.environ.get("RAG_RETRIEVAL_BUDGET_MS", "2500"))
    retrieval_budget_s = max(0.0, retrieval_budget_ms / 1000.0)

    def budget_exceeded() -> bool:
        return retrieval_budget_s > 0 and (time.perf_counter() - started) >= retrieval_budget_s

    # Sizing the candidate pools.
    base_top_n = top_n or MAX_CONTEXT_CHUNKS
    multi_doc = len(doc_ids) > 1
    # Phase 3 — record the shared retrieval plan (routing decision) for telemetry.
    plan = build_retrieval_plan(analysis, doc_count=len(doc_ids))
    diagnostics["retrieval_plan"] = plan.to_dict()
    if requires_all_docs and multi_doc:
        # Coverage mode makes sense only with multiple documents: ensure at
        # least one chunk per document.  For single-document sessions we keep
        # the caller-supplied limits so "give me a summary" still retrieves
        # enough chunks instead of being hard-capped to 1.
        base_top_n = max(base_top_n, len(doc_ids))
        total_limit = max(total_limit or base_top_n, len(doc_ids))
        max_chunks_per_doc = 1
    elif (
        RAG_RECALL_MODE
        and multi_doc
        and analysis.coverage_mode == COVERAGE_MODE_BALANCED
    ):
        # Phase 1 — adaptive budget for distributed tasks (panorama / comparative
        # / "X de cada documento"): guarantee room for ~PER_DOC_FLOOR chunks per
        # in-scope document instead of a fixed cap, bounded by RAG_MAX_CONTEXT_CHUNKS.
        adaptive_floor = min(
            len(doc_ids) * max(1, RAG_PER_DOC_FLOOR), RAG_MAX_CONTEXT_CHUNKS
        )
        base_top_n = max(base_top_n, adaptive_floor)
        total_limit = max(total_limit or base_top_n, adaptive_floor)
        diagnostics["adaptive_budget"] = adaptive_floor
    if retrieval_mode == "light":
        base_top_n = max(2, min(base_top_n, 4))
        vector_multiplier = min(RAG_VECTOR_POOL_MULTIPLIER, 1.25)
        rerank_pool = min(RAG_RERANK_POOL, 10)
    else:
        vector_multiplier = RAG_VECTOR_POOL_MULTIPLIER
        rerank_pool = RAG_RERANK_POOL

    pool_top_n = max(base_top_n, rerank_pool)
    vector_per_query = max(
        base_top_n,
        int(round(base_top_n * vector_multiplier)),
    )
    lexical_multiplier = RAG_LEXICAL_POOL_MULTIPLIER
    if analysis.query_type == QUERY_TYPE_NUMERIC:
        # Numeric queries tend to benefit from stronger lexical recall.
        lexical_multiplier = max(lexical_multiplier, 2.8)
    lexical_per_query = max(
        base_top_n,
        int(round(base_top_n * lexical_multiplier)),
    )
    if retrieval_mode == "light":
        lexical_per_query = min(lexical_per_query, max(4, base_top_n + 2))

    # Strategy matrix based on query analysis (never on corpus size alone).
    # Phase 5: an explicit caller override (skills / evaluations) wins so the
    # unified retrieval path can still honour stack-specific configuration.
    if retrieval_strategy in {"global", "hybrid_per_document"}:
        vector_strategy = retrieval_strategy
    elif requires_all_docs:
        vector_strategy = "hybrid_per_document"
    elif analysis.query_type == QUERY_TYPE_PANORAMA:
        vector_strategy = "hybrid_per_document"
    elif analysis.query_type in {QUERY_TYPE_COMPARATIVE, QUERY_TYPE_NUMERIC}:
        vector_strategy = "global"
    else:
        vector_strategy = "global"

    # --- Vector retrieval (per sub-query) ---
    vector_lists: list[list[SmartChunk]] = []
    for q in queries:
        if budget_exceeded():
            diagnostics["retrieval_timed_out"] = True
            diagnostics["retrieval_skipped_reason"] = "budget_exceeded"
            break
        try:
            vector_total_limit = len(doc_ids) if (requires_all_docs and multi_doc) else vector_per_query
            vector_k_per_doc = 1 if (requires_all_docs and multi_doc) else k_per_doc
            vector_max_per_doc = 1 if (requires_all_docs and multi_doc) else max(2, k_per_doc + 1)
            v_chunks = fetch_relevant_chunks(
                user=user,
                query_text=q,
                allowed_documents=allowed_documents,
                top_n=vector_per_query,
                topics=topics,
                retrieval_strategy=vector_strategy,
                k_per_doc=vector_k_per_doc,
                total_limit=vector_total_limit,
                max_chunks_per_doc=vector_max_per_doc,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Vector retrieval failed for sub-query %r: %s", q, exc)
            v_chunks = []
        if v_chunks:
            vector_lists.append(v_chunks)
            diagnostics["vector_candidates"] += len(v_chunks)

    # --- Lexical retrieval (single pass on original query) ---
    base_qs = _scope_qs_for_user(user, doc_ids)
    if budget_exceeded():
        diagnostics["retrieval_timed_out"] = True
        diagnostics["retrieval_skipped_reason"] = "budget_exceeded"
        lex_chunks = []
    else:
        try:
            lex_chunks = lexical_search(base_qs, query_text, top_n=lexical_per_query)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Lexical retrieval failed: %s", exc)
            lex_chunks = []
    diagnostics["lexical_candidates"] = len(lex_chunks)

    # --- Fusion ---
    ranked_lists: list[list[SmartChunk]] = []
    ranked_lists.extend(vector_lists)
    if lex_chunks:
        ranked_lists.append(lex_chunks)

    if not ranked_lists:
        fallback_limit = total_limit or base_top_n
        fallback_chunks = _single_doc_retrieval_fallback(
            user,
            doc_ids,
            query_text,
            total_limit=fallback_limit,
        )
        if fallback_chunks:
            diagnostics["single_doc_fallback"] = True
            diagnostics["final_chunks"] = len(fallback_chunks)
            diagnostics["unique_documents"] = 1
            diagnostics["retrieval_confidence"] = "low"
            diagnostics["elapsed_seconds"] = round(time.perf_counter() - started, 4)
            return RetrievalResult(
                chunks=fallback_chunks,
                context_block=_build_context_block(fallback_chunks, with_citations=True),
                analysis=analysis,
                diagnostics=diagnostics,
            )
        diagnostics["elapsed_seconds"] = time.perf_counter() - started
        return RetrievalResult(analysis=analysis, diagnostics=diagnostics)

    fused = rrf_fuse(ranked_lists, top_n=pool_top_n)
    diagnostics["fused_candidates"] = len(fused)

    # --- Optional LLM rerank ---
    safe_total = total_limit or base_top_n
    if budget_exceeded():
        diagnostics["retrieval_timed_out"] = True
        diagnostics["retrieval_skipped_reason"] = "budget_exceeded"
    elif (
        retrieval_mode != "light"
        and is_reranker_enabled()
        and len(fused) > safe_total
        and analysis.query_type in {QUERY_TYPE_PANORAMA, QUERY_TYPE_COMPARATIVE}
    ):
        fused = llm_rerank(query_text, fused, top_k=min(len(fused), pool_top_n))
        diagnostics["reranked"] = True

    # --- Diversity / per-doc cap ---
    safe_per_doc = max_chunks_per_doc if max_chunks_per_doc else max(1, min(3, k_per_doc + 1))
    capped = cap_per_document(
        fused,
        max_per_doc=safe_per_doc,
        total_limit=safe_total,
    )

    if requires_all_docs and multi_doc:
        covered_ids = {c.document_id for c in capped}
        target_count = max(1, int(round(len(doc_ids) * RAG_ALL_DOCS_MIN_COVERAGE_RATIO)))
        missing_doc_ids = [doc_id for doc_id in doc_ids if doc_id not in covered_ids]
        diagnostics["coverage_target_documents"] = target_count
        if len(covered_ids) < target_count and missing_doc_ids:
            capped = _fill_missing_documents(
                base_qs=base_qs,
                query_text=query_text,
                missing_doc_ids=missing_doc_ids,
                existing_chunks=capped,
            )
            capped = cap_per_document(
                capped,
                max_per_doc=1,
                total_limit=max(safe_total, target_count),
            )

    # --- Optional MMR (off by default) ---
    final_chunks: list[SmartChunk] = capped
    if budget_exceeded():
        diagnostics["retrieval_timed_out"] = True
        diagnostics["retrieval_skipped_reason"] = "budget_exceeded"
    elif retrieval_mode != "light" and is_mmr_enabled() and capped:
        try:
            q_emb = embed_text(query_text)
            final_chunks = mmr_select(capped, q_emb, top_k=safe_total)
            diagnostics["mmr_applied"] = True
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("MMR selection failed, falling back to capped: %s", exc)
            final_chunks = capped

    # --- Relevance threshold / confidence labeling ---
    # Phase 1 (RAG_RECALL_MODE): the absolute similarity cutoff no longer *drops*
    # evidence — dropping below a fixed cosine threshold is exactly what made
    # real-but-low-similarity passages disappear ("el dato existía pero el RAG lo
    # tiró"). We keep the budget-capped ranked set and use the threshold only to
    # *label* confidence. Legacy hard-drop is available via RAG_RECALL_MODE=0.
    if final_chunks:
        scored_similarities = [
            s for s in (_chunk_similarity(c) for c in final_chunks) if s is not None
        ]

        if not RAG_RECALL_MODE:
            final_chunks = [
                c
                for c in final_chunks
                if _chunk_similarity(c) is None
                or _chunk_similarity(c) >= RAG_MIN_SIMILARITY
            ]
        else:
            diagnostics["below_threshold_kept"] = sum(
                1 for s in scored_similarities if s < RAG_MIN_SIMILARITY
            )

        if scored_similarities:
            max_similarity = max(scored_similarities)
            avg_similarity = sum(scored_similarities) / len(scored_similarities)
            diagnostics["max_similarity"] = round(max_similarity, 4)
            diagnostics["avg_similarity"] = round(avg_similarity, 4)
            if max_similarity >= 0.78:
                diagnostics["retrieval_confidence"] = "high"
            elif max_similarity >= 0.55:
                diagnostics["retrieval_confidence"] = "medium"
            elif max_similarity >= RAG_MIN_SIMILARITY:
                diagnostics["retrieval_confidence"] = "low"
            else:
                diagnostics["retrieval_confidence"] = "none"
        elif final_chunks:
            diagnostics["retrieval_confidence"] = "low"

    if not final_chunks and len(doc_ids) == 1:
        fallback_chunks = _single_doc_retrieval_fallback(
            user,
            doc_ids,
            query_text,
            total_limit=safe_total,
        )
        if fallback_chunks:
            final_chunks = fallback_chunks
            diagnostics["single_doc_fallback"] = True
            diagnostics["retrieval_confidence"] = "low"

    # --- Phase 1: small-to-big parent expansion ---
    # Widen each surviving chunk with its neighbours (same document, adjacent
    # chunk_index) so the model reads a contiguous passage instead of an
    # isolated ~500-token fragment. Anchors are never dropped.
    if RAG_PARENT_EXPANSION and final_chunks and not budget_exceeded():
        try:
            expanded = _expand_to_parents(
                final_chunks,
                user,
                window=RAG_PARENT_WINDOW,
                max_total=max(safe_total, RAG_MAX_CONTEXT_CHUNKS),
            )
            if expanded:
                diagnostics["parent_expansion"] = {
                    "anchors": len(final_chunks),
                    "expanded": len(expanded),
                    "window": RAG_PARENT_WINDOW,
                }
                final_chunks = expanded
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Parent expansion failed: %s", exc)

    context_block = _build_context_block(final_chunks, with_citations=True)

    diagnostics["elapsed_seconds"] = round(time.perf_counter() - started, 4)
    diagnostics["final_chunks"] = len(final_chunks)
    diagnostics["unique_documents"] = len({c.document_id for c in final_chunks})
    final_doc_ids = {c.document_id for c in final_chunks}
    missing_doc_ids = [doc_id for doc_id in doc_ids if doc_id not in final_doc_ids]
    diagnostics["coverage_missing_documents"] = missing_doc_ids
    if requires_all_docs:
        target_count = diagnostics["coverage_target_documents"] or len(doc_ids)
        diagnostics["coverage_met"] = len(final_doc_ids) >= target_count

    return RetrievalResult(
        chunks=final_chunks,
        context_block=context_block,
        analysis=analysis,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# Backward-compatible re-exports
# ---------------------------------------------------------------------------


def build_context_block(chunks: Iterable[SmartChunk]) -> str:
    """Backward-compatible wrapper. New callers should use the orchestrator."""
    return _build_context_block(chunks, with_citations=True)


__all__ = [
    "MAX_CONTEXT_CHUNKS",
    "RetrievalResult",
    "build_citation_prompt",
    "build_context_block",
    "fetch_relevant_chunks",
    "is_general_query",
    "retrieve_for_chat",
    "retrieve_from_library",
    "suggest_related_library_documents",
]
