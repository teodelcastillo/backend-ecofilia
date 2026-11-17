from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
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
from apps.document.models import Document, SmartChunk
from apps.document.utils.client_openia import generate_chat_completion
from apps.evaluation.models import (
    EvaluationRun,
    EvaluationRunStatus,
    MetricEvaluationResult,
    MetricResponseType,
    PillarEvaluationResult,
)
from apps.skill.models import Skill, SkillExecution, ExecutionStatus
from apps.skill.services import execute_skill, execution_to_markdown

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_CHUNKS = int(
    os.environ.get("EVALUATION_CONTEXT_CHUNKS", os.environ.get("CHAT_CONTEXT_CHUNKS", "8"))
)
EVALUATION_MAX_WORKERS = int(os.environ.get("EVALUATION_MAX_WORKERS", "6"))


class EvaluationRunner:
    """
    Ejecuta una evaluación pillar → métricas utilizando RAG + LLM.
    """

    def __init__(self, *, chunks_per_metric: int | None = None):
        self.chunks_per_metric = chunks_per_metric or DEFAULT_CONTEXT_CHUNKS

    def run(self, run_id: int) -> EvaluationRun:
        run = (
            EvaluationRun.objects.select_related("evaluation", "owner", "project")
            .prefetch_related("evaluation__pillars__metrics")
            .get(pk=run_id)
        )
        if run.status not in (EvaluationRunStatus.PENDING, EvaluationRunStatus.FAILED):
            return run

        documents_qs = self._documents_for_run(run)
        if not documents_qs.exists():
            message = "La ejecución no tiene documentos asociados."
            logger.error("Run %s aborted: %s", run.id, message)
            run.status = EvaluationRunStatus.FAILED
            run.error_message = message
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error_message", "finished_at"])
            return run

        logger.info("Starting evaluation run %s with %s documents.", run.id, documents_qs.count())
        run.status = EvaluationRunStatus.RUNNING
        run.error_message = ""
        run.started_at = timezone.now()
        run.pillar_results.all().delete()
        run.save(update_fields=["status", "error_message", "started_at"])

        try:
            self._process_pillars(run, documents_qs)
        except Exception as exc:  # pragma: no cover - defensive, tested via mocks
            logger.exception("Evaluation run %s failed", run.id)
            run.status = EvaluationRunStatus.FAILED
            run.error_message = str(exc)
        else:
            run.status = EvaluationRunStatus.COMPLETED
        finally:
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error_message", "finished_at"])

        return run

    def _process_pillars(self, run: EvaluationRun, documents_qs):
        evaluation = run.evaluation

        # Pre-load all pillars and their metrics before parallelizing to avoid
        # queryset lazy-evaluation inside threads (which can race on the prefetch cache).
        pillar_data: list[tuple] = []
        for pillar in evaluation.pillars.all():
            metrics = list(pillar.metrics.all())
            pillar_data.append((pillar, metrics))

        # Pre-create all PillarEvaluationResult rows (fast, sequential).
        pillar_result_map: dict[int, PillarEvaluationResult] = {}
        for pillar, _ in pillar_data:
            pr = PillarEvaluationResult.objects.create(
                run=run,
                pillar=pillar,
                position=pillar.position,
                summary="",
            )
            pillar_result_map[pillar.id] = pr

        # Flatten all (pillar, metric) pairs into a single task list.
        tasks = [
            (pillar, metric)
            for pillar, metrics in pillar_data
            for metric in metrics
        ]

        if not tasks:
            return

        # Run all metric evaluations in parallel — the expensive part is the
        # RAG retrieval + LLM call inside _evaluate_metric, which is pure I/O.
        # DB writes happen after this block so there are no write-concurrency issues.
        max_workers = min(len(tasks), EVALUATION_MAX_WORKERS)
        metric_results: dict[tuple[int, int], MetricEvaluationResult] = {}
        metric_errors: dict[tuple[int, int], Exception] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(
                    self._evaluate_metric,
                    run=run,
                    pillar=pillar,
                    metric=metric,
                    documents_qs=documents_qs,
                ): (pillar.id, metric.id)
                for pillar, metric in tasks
            }
            for future in as_completed(future_map):
                key = future_map[future]
                try:
                    metric_results[key] = future.result()
                except Exception as exc:
                    metric_errors[key] = exc
                    pillar_id, metric_id = key
                    logger.exception(
                        "Metric %s in pillar %s failed during parallel evaluation",
                        metric_id, pillar_id,
                    )

        # Una métrica que falla no tumba la corrida: las demás siguen valiendo.
        # Pero si fallaron todas no hay evaluación — típicamente el modelo está
        # caído —, y marcarla `completed` (como hacía desde que las métricas
        # corren en paralelo) muestra una corrida vacía como si hubiera salido
        # bien. Se levanta para que `run()` la deje FAILED con el motivo.
        if metric_errors and not metric_results:
            first_error = next(iter(metric_errors.values()))
            raise RuntimeError(
                f"Fallaron las {len(metric_errors)} métricas de la evaluación. "
                f"Primer error: {first_error}"
            ) from first_error

        # Collect results and persist per pillar (sequential, fast).
        for pillar, metrics in pillar_data:
            pillar_result = pillar_result_map[pillar.id]
            pillar_chunk_ids: List[int] = []
            pillar_sources: List[dict] = []

            for metric in metrics:
                key = (pillar.id, metric.id)
                metric_result = metric_results.get(key)
                if metric_result is None:
                    logger.warning(
                        "No result recorded for metric %s in pillar %s — skipping.",
                        metric.id, pillar.id,
                    )
                    continue
                metric_result.pillar_result = pillar_result
                metric_result.position = metric.position
                metric_result.save()
                pillar_chunk_ids.extend(metric_result.chunk_ids or [])
                pillar_sources.extend(metric_result.sources or [])

            pillar_result.summary = pillar.context_instructions or f"Resultados del pilar {pillar.title}."
            pillar_result.chunk_ids = pillar_chunk_ids
            pillar_result.sources = pillar_sources
            pillar_result.save(update_fields=["summary", "chunk_ids", "sources"])

    def _evaluate_metric(self, *, run, pillar, metric, documents_qs):
        skill_evidence = self._try_linked_skill_evidence(
            run=run,
            metric=metric,
            documents_qs=documents_qs,
        )
        if skill_evidence is not None:
            return skill_evidence

        # ── Phase 1: decompose the metric into focused retrieval queries ──────
        retrieval_queries = self._build_retrieval_queries(pillar, metric, run)

        # ── Phase 2: hybrid retrieval + RRF cross-query ───────────────────────
        doc_ids = list(documents_qs.values_list("id", flat=True))
        doc_count = len(doc_ids)
        # Phase 3 brain adoption: multi-doc defaults to per-document; a distributed
        # metric query (comparative / per-entity) upgrades a single-doc run too.
        retrieval_strategy = recommend_strategy(
            retrieval_queries[0] if retrieval_queries else "",
            default="hybrid_per_document" if doc_count > 1 else "global",
        )
        base_qs = SmartChunk.objects.filter(
            document_id__in=doc_ids
        ).exclude(embedding__isnull=True)

        # Phase 5: unified retrieval — one call to the shared engine
        # (retrieve_for_chat) instead of a bespoke per-query vector+lexical+RRF
        # loop. The decomposed metric queries are joined so the engine's own
        # expansion still pulls multiple anchors.
        combined_query = " ".join(q for q in retrieval_queries if q).strip()
        if not combined_query and retrieval_queries:
            combined_query = retrieval_queries[0]
        try:
            retrieval = retrieve_for_chat(
                user=run.owner,
                query_text=combined_query,
                allowed_documents=documents_qs,
                top_n=self.chunks_per_metric,
                total_limit=self.chunks_per_metric,
                k_per_doc=3,
                retrieval_strategy=retrieval_strategy,
            )
            final_chunks = list(retrieval.chunks)
        except Exception as exc:
            logger.warning("Metric %s retrieval failed: %s", metric.id, exc)
            final_chunks = []

        # Evidence quality from chunk similarity (anchors carry distance/lex_sim;
        # parent-expansion neighbours are unscored and simply omitted from sims).
        sims = [s for s in (_chunk_similarity(c) for c in final_chunks) if s is not None]
        max_sim = max(sims) if sims else None
        above = sum(1 for s in sims if s >= RAG_MIN_SIMILARITY)
        evidence_quality: dict = {
            "chunks_retrieved": len(final_chunks),
            "chunks_above_threshold": above,
            "avg_similarity": round(sum(sims) / len(sims), 4) if sims else None,
            "max_similarity": round(max_sim, 4) if max_sim is not None else None,
            "evidence_level": (
                "high" if (max_sim is not None and max_sim >= 0.7)
                else "medium" if (max_sim is not None and max_sim >= 0.4)
                else "low"
            ),
            "docs_covered": len({c.document_id for c in final_chunks}),
            "docs_in_scope": doc_count,
        }

        # ── Phase 3: optional LLM reranker ───────────────────────────────────
        if (
            os.environ.get("EVALUATION_RERANKER_ENABLED", "0").strip().lower()
            in ("1", "true", "yes")
            and final_chunks
        ):
            from apps.chat.services.reranker import llm_rerank
            final_chunks = llm_rerank(
                retrieval_queries[0], final_chunks, top_k=self.chunks_per_metric
            )

        context_block = build_context_block(final_chunks)
        is_quantitative = metric.response_type == MetricResponseType.QUANTITATIVE
        prompt = self._build_prompt(
            run, pillar, metric, context_block, evidence_quality=evidence_quality
        )
        messages = [
            {"role": "system", "content": run.evaluation.system_prompt},
            {"role": "user", "content": prompt},
        ]
        completion_kwargs: dict = {
            "model": run.model,
            "temperature": run.temperature,
        }
        if is_quantitative:
            # Force JSON output for numeric metrics so we never lose the score
            # to regex parsing failures on prose like "entre 3 y 4 puntos".
            completion_kwargs["response_format"] = {"type": "json_object"}

        response_text, usage = generate_chat_completion(messages, **completion_kwargs)

        response_value = None
        if is_quantitative:
            try:
                parsed = json.loads(response_text)
                raw_score = parsed.get("score")
                if raw_score is not None:
                    response_value = float(raw_score)
                    if metric.scale_min is not None and response_value < metric.scale_min:
                        response_value = None
                    elif metric.scale_max is not None and response_value > metric.scale_max:
                        response_value = None
                # Replace response_text with the qualitative justification so the
                # UI shows readable analysis rather than raw JSON.
                response_text = parsed.get("justification") or parsed.get("analysis") or response_text
            except (json.JSONDecodeError, ValueError, TypeError):
                logger.warning(
                    "Metric %s: JSON parse failed on quantitative response — falling back to regex.",
                    metric.id,
                )
                response_value = self._extract_numeric_value(
                    response_text, metric.scale_min, metric.scale_max
                )

        chunk_ids = [chunk.id for chunk in final_chunks]
        sources = [
            {
                "chunk_id": chunk.id,
                "document_slug": chunk.document.slug,
                "document_name": chunk.document.name,
                "chunk_index": chunk.chunk_index,
                "distance": getattr(chunk, "distance", None),
            }
            for chunk in final_chunks
        ]

        metric_result = MetricEvaluationResult(
            metric=metric,
            response_type=metric.response_type,
            response_text=response_text,
            response_value=response_value,
            chunk_ids=chunk_ids,
            sources=sources,
            metadata={"usage": usage, "evidence_quality": evidence_quality},
        )
        return metric_result

    def _try_linked_skill_evidence(self, *, run, metric, documents_qs):
        linked_slug = (metric.linked_skill_slug or "").strip()
        if not linked_slug:
            return None

        try:
            skill = Skill.objects.get(slug=linked_slug)
        except Skill.DoesNotExist:
            logger.warning(
                "Metric %s references missing skill slug '%s'",
                metric.id,
                linked_slug,
            )
            return None

        doc_slugs = list(documents_qs.values_list("slug", flat=True))
        execution_kwargs = {
            "skill": skill,
            "owner": run.owner,
            "metadata": {},
        }
        if run.project_id:
            execution_kwargs["project_id"] = run.project_id
        if doc_slugs:
            execution_kwargs["metadata"]["document_slugs_filter"] = doc_slugs
        elif skill.pinned_document_slugs:
            execution_kwargs["metadata"]["document_slugs_filter"] = skill.pinned_document_slugs

        execution = SkillExecution.objects.create(**execution_kwargs)
        execution = execute_skill(execution)

        if execution.status != ExecutionStatus.COMPLETED:
            logger.warning(
                "Linked skill '%s' for metric %s finished with status %s",
                linked_slug,
                metric.id,
                execution.status,
            )
            return None

        response_text = execution_to_markdown(execution)
        response_value = None
        if metric.response_type == MetricResponseType.QUANTITATIVE:
            response_value = self._extract_numeric_value(
                response_text, metric.scale_min, metric.scale_max
            )

        sources = [
            {
                "document_slug": entry.get("slug"),
                "document_name": entry.get("name"),
                "chunk_index": None,
            }
            for entry in (execution.document_snapshot or [])
            if entry.get("slug")
        ]

        return MetricEvaluationResult(
            metric=metric,
            response_type=metric.response_type,
            response_text=response_text,
            response_value=response_value,
            chunk_ids=[],
            sources=sources,
            metadata={
                "linked_skill_execution_id": execution.id,
                "linked_skill_slug": skill.slug,
                "linked_skill_name": skill.name,
            },
        )

    def _documents_for_run(self, run: EvaluationRun):
        snapshot = run.document_snapshot or []
        doc_ids = [entry.get("id") for entry in snapshot if entry.get("id")]
        return Document.objects.filter(id__in=doc_ids)

    def _build_query_text(self, pillar, metric, run):
        parts = [
            metric.instructions or "",
            metric.criteria or "",
            pillar.context_instructions or "",
            run.instructions_override or "",
        ]
        return "\n".join(part for part in parts if part).strip()

    def _build_retrieval_queries(self, pillar, metric, run) -> list[str]:
        primary = f"{metric.instructions or ''} {metric.criteria or ''}".strip()[:400]
        secondary = (pillar.context_instructions or "")[:200].strip()
        queries = [q for q in [primary, secondary] if q]
        if len(primary.split()) > 15:
            from apps.chat.services.query_analysis import _extract_keywords
            kws = _extract_keywords(primary, max_terms=8)
            if kws:
                queries.append(" ".join(kws))
        return queries[:3]

    def _build_prompt(self, run, pillar, metric, context_block: str, *, evidence_quality: dict | None = None) -> str:
        language = run.language or "es"
        lines = [
            f"Evaluación: {run.evaluation.title}",
            f"Pilar: {pillar.title}",
        ]
        if pillar.context_instructions:
            lines.append(f"Contexto del pilar:\n{pillar.context_instructions}")
        lines.append(f"Instrucciones del KPI:\n{metric.instructions}")
        if metric.criteria:
            lines.append(f"Criterios adicionales:\n{metric.criteria}")
        if metric.response_type == MetricResponseType.QUANTITATIVE:
            scale = "El resultado debe ser numérico"
            if metric.scale_min is not None and metric.scale_max is not None:
                scale += f" entre {metric.scale_min} y {metric.scale_max}"
            scale += "."
            if metric.expected_units:
                scale += f" Unidades esperadas: {metric.expected_units}."
            lines.append(scale)
            lines.append(
                'Responde ÚNICAMENTE con JSON válido en la forma: '
                '{"score": <número>, "justification": "<análisis cualitativo que respalda el score>"}'
            )
        else:
            lines.append("Entrega un análisis cualitativo/descriptivo.")

        if run.instructions_override:
            lines.append(f"Instrucciones adicionales del usuario:\n{run.instructions_override}")

        if evidence_quality and evidence_quality.get("evidence_level") == "low":
            lines.append(
                "ADVERTENCIA: La evidencia documental recuperada es escasa o de baja similitud. "
                "Indica explícitamente en tu respuesta qué información falta para una evaluación completa."
            )

        if context_block:
            lines.append(
                "Al citar evidencia usa la notación [#N] referenciando el número del fragmento. "
                "Contexto documental:\n" + context_block
            )
        else:
            lines.append("No se encontraron fragmentos relevantes; indica claramente si falta contexto.")

        lines.append(f"Responde en idioma: {language}. Referencia siempre las fuentes proporcionadas.")
        return "\n\n".join(lines)

    def _extract_numeric_value(self, text: str, scale_min, scale_max):
        if not text:
            return None
        matches = re.findall(r"-?\d+(?:[.,]\d+)?", text)
        for match in matches:
            value = float(match.replace(",", "."))
            if scale_min is not None and value < scale_min:
                continue
            if scale_max is not None and value > scale_max:
                continue
            return value
        return None


def execute_evaluation_run(run: EvaluationRun) -> EvaluationRun:
    runner = EvaluationRunner()
    return runner.run(run.id)


def apply_skill_execution_to_metric(
    *,
    run: EvaluationRun,
    metric_id: int,
    execution: SkillExecution,
) -> MetricEvaluationResult:
    """
    Insert a completed skill execution's output as evidence on an existing
    metric result within an evaluation run.
    """
    if execution.status != ExecutionStatus.COMPLETED:
        raise ValueError("Skill execution must be completed before applying as evidence.")

    metric_result = (
        MetricEvaluationResult.objects.select_related("metric")
        .filter(
            pillar_result__run=run,
            metric_id=metric_id,
        )
        .first()
    )
    if metric_result is None:
        raise ValueError("Metric result not found for this evaluation run.")

    response_text = execution_to_markdown(execution)
    metric_result.response_text = response_text
    if metric_result.metric.response_type == MetricResponseType.QUANTITATIVE:
        runner = EvaluationRunner()
        metric_result.response_value = runner._extract_numeric_value(
            response_text,
            metric_result.metric.scale_min,
            metric_result.metric.scale_max,
        )

    metadata = dict(metric_result.metadata or {})
    metadata["linked_skill_execution_id"] = execution.id
    metadata["linked_skill_slug"] = execution.skill.slug
    metadata["linked_skill_name"] = execution.skill.name
    metadata["applied_from_skill_execution"] = True
    metric_result.metadata = metadata

    skill_sources = [
        {
            "document_slug": entry.get("slug"),
            "document_name": entry.get("name"),
            "chunk_index": None,
        }
        for entry in (execution.document_snapshot or [])
        if entry.get("slug")
    ]
    if skill_sources:
        metric_result.sources = skill_sources

    metric_result.save()
    return metric_result
