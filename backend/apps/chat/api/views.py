from __future__ import annotations

import json
import logging
import os
import re

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import StreamingHttpResponse
from rest_framework import mixins, status, viewsets
from rest_framework.pagination import PageNumberPagination
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.chat.api.serializers import (
    ChatMessageCreateSerializer,
    ChatMessageSerializer,
    ChatSessionCreateSerializer,
    ChatSessionSerializer,
    prefetch_chunks_by_id,
)
from apps.chat.models import ChatMessage, ChatSession, MessageRole, touch_chat_session_activity
from apps.chat.services.context_builder import build_citation_prompt
from apps.chat.services.query_analysis import (
    COVERAGE_MODE_ALL,
    QUERY_TYPE_COMPARATIVE,
    QUERY_TYPE_PANORAMA,
    apply_response_mode_override,
    classify_query,
    classify_query_hybrid,
    contextualize_query,
)
from apps.chat.services.rag import RetrievalResult, retrieve_for_chat, retrieve_from_library, suggest_related_library_documents
from apps.chat.services.fanout import apply_fanout, maybe_fanout
from apps.document.models import Document
from apps.document.utils import client_openia
from apps.document.utils.llm import effective_chat_model

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = int(os.environ.get("CHAT_HISTORY_MESSAGES", "10"))
CITATION_PATTERN = re.compile(r"\[#(\d+)\]")

# Marker used to detect sessions whose stored system_prompt was created with
# the old RAG-only default.  When no context is retrieved (greetings, trivial
# queries, library gap) that prompt causes the LLM to reply
# "No tengo información disponible."  We swap it for a conversational fallback.
_RAG_CONTEXT_REQUIRED_MARKER = "Responde únicamente con la información disponible"
_CONVERSATIONAL_FALLBACK_PROMPT = (
    "Eres Ecofilia, un asistente experto en ESG y sostenibilidad. "
    "Responde de forma clara, amable y concisa. "
    "Si el usuario saluda o hace una consulta general, responde cordialmente. "
    "Si necesita información específica de documentos, sugiérele que seleccione "
    "documentos de la biblioteca o formule una pregunta más concreta."
)
_SINGLE_DOC_NO_CONTEXT_PROMPT = (
    "Eres Ecofilia, un asistente especializado en análisis documental. "
    'El usuario está analizando el documento "{doc_name}". '
    "Todas las preguntas se refieren exclusivamente a este documento. "
    "No pidas al usuario que cargue, suba ni seleccione documentos: ya está en una "
    "sesión acotada a este archivo. "
    "Si no hay contexto recuperado para la consulta, indícalo claramente y sugiere "
    "reformular la pregunta de forma más concreta sobre el contenido del documento."
)

# ---------------------------------------------------------------------------
# Query contextualization guard
# ---------------------------------------------------------------------------
# Calling contextualize_query() costs ~2-5s (LLM round-trip). We only need
# it when the query contains anaphoric references that can't be resolved
# without conversation history. Self-contained questions (specific country,
# year, metric — no pronouns or dangling references) are used as-is.

_ANAPHORA_STARTERS = re.compile(
    r"^(y\b|¿y\b|también\b|tambien\b|además\b|ademas\b|pero\b|"
    r"sin embargo\b|al respecto\b|sobre eso\b|en ese caso\b|"
    r"con respecto a eso\b|en relación\b|en relacion\b)",
    re.IGNORECASE | re.UNICODE,
)
_ANAPHORA_PRONOUNS = re.compile(
    r"\b(él|ella|ellos|ellas|eso|esto|esos|esas|ese|esta(?!\s+\w+\s+(país|region|zona))|"
    r"el mismo|la misma|los mismos|las mismas|"
    r"dicho|dichos|dichas|mencionado|mencionada|mencionados|mencionadas|"
    r"anterior|lo anterior|lo mencionado|lo dicho|el tema|el punto)\b",
    re.IGNORECASE | re.UNICODE,
)
_SUBJECTLESS_VERBS = re.compile(
    r"^(dame\b|dime\b|explicame\b|explícame\b|cuéntame\b|cuentame\b|"
    r"amplía\b|amplia\b|profundiza\b|detalla\b|resume\b|comparalo\b|"
    r"compáralo\b|compárala\b)",
    re.IGNORECASE | re.UNICODE,
)


def _query_needs_context(text: str) -> bool:
    """
    Return True when the query contains anaphoric references that require
    conversation history to be resolved — contextualize_query() should run.
    Return False when the query is self-contained — skip the LLM call.

    Conservative by design: ambiguous cases return True (call the LLM).
    Catches the most common patterns: dangling pronouns, anaphoric starters,
    subjectless verbs, and very short elliptic follow-ups.
    """
    norm = (text or "").strip()
    # Very short queries are almost always elliptic follow-ups.
    if len(norm.split()) < 4:
        return True
    norm_lower = norm.lower()
    if _ANAPHORA_STARTERS.match(norm_lower):
        return True
    if _ANAPHORA_PRONOUNS.search(norm_lower):
        return True
    if _SUBJECTLESS_VERBS.match(norm_lower):
        return True
    return False


def _chat_retrieval_params(
    session: ChatSession,
    content: str,
    response_mode: str | None = None,
    *,
    doc_count_override: int | None = None,
) -> dict:
    """
    Heuristic sizing for the RAG retrieval pool given the session scope and
    the current message. Wider pools for broader questions and bigger libraries.
    """
    doc_count = (
        doc_count_override if doc_count_override is not None else session.allowed_documents.count()
    )
    # Use fast regex classifier for pool sizing — the full LLM-quality
    # classification runs once inside retrieve_for_chat.
    analysis = classify_query(content)
    analysis = apply_response_mode_override(analysis, response_mode)
    broad_question = len((content or "").split()) >= 18
    if analysis.coverage_mode == COVERAGE_MODE_ALL:
        total_limit = doc_count
        max_chunks_per_doc = 1
    elif doc_count == 1:
        # Single-document chat: retrieve more chunks for richer context
        total_limit = 10
        max_chunks_per_doc = 10
    else:
        total_limit = min(18, max(8, doc_count * 2))
        max_chunks_per_doc = 2
    if not broad_question and analysis.coverage_mode != COVERAGE_MODE_ALL and doc_count > 1:
        total_limit = min(total_limit, 12)
    return {
        "top_n": total_limit,
        "total_limit": total_limit,
        "k_per_doc": max_chunks_per_doc,
        "max_chunks_per_doc": max_chunks_per_doc,
    }


def _build_coverage_instruction(session: ChatSession, retrieval: RetrievalResult) -> str:
    if retrieval.analysis is None or retrieval.analysis.coverage_mode != COVERAGE_MODE_ALL:
        return ""

    docs = list(session.allowed_documents.order_by("name").values("id", "name", "slug"))
    total_docs = len(docs)
    covered_ids = retrieval.covered_document_ids
    missing = [doc for doc in docs if doc["id"] not in covered_ids]
    covered_count = total_docs - len(missing)
    missing_text = (
        " Documentos sin evidencia recuperada: "
        + ", ".join(f"{doc['name']} ({doc['slug']})" for doc in missing)
        + "."
        if missing
        else ""
    )
    return (
        "\n\nPOLITICA DE COBERTURA OBLIGATORIA:\n"
        f"- La sesión tiene {total_docs} documentos seleccionados.\n"
        f"- El contexto recuperado cubre {covered_count}/{total_docs} documentos.\n"
        "- Para preguntas de panorama/repositorio/base documental, debes razonar "
        "sobre todos los documentos cubiertos y no presentar una respuesta como "
        "completa si falta cobertura.\n"
        "- Si el usuario pide listar o resumir por documento, devuelve exactamente "
        "una entrada por cada documento cubierto, sin repetir documentos.\n"
        "- Si hay documentos sin evidencia recuperada, menciónalos explícitamente "
        "como no cubiertos en esta respuesta."
        f"{missing_text}"
    )


def _workspace_session(session: ChatSession) -> bool:
    """Sesión ligada a proyecto o repositorio: tiene sentido sugerir docs de la biblioteca global."""
    return session.project_id is not None or session.repository_id is not None


def _extract_citation_payload(answer_text: str, retrieval: RetrievalResult) -> dict:
    """
    Build citation metadata from inline markers [#N].
    Returns stable mapping data for UI traceability.
    """
    chunks = list(retrieval.chunks or [])
    retrieval_chunk_ids = [chunk.id for chunk in chunks]
    if not retrieval_chunk_ids:
        return {
            "chunk_ids": [],
            "citations": [],
            "retrieval_chunk_ids": [],
            "citation_integrity": "missing",
        }

    cited_positions: list[int] = []
    for match in CITATION_PATTERN.findall(answer_text or ""):
        try:
            idx = int(match) - 1
        except ValueError:
            continue
        if 0 <= idx < len(retrieval_chunk_ids):
            cited_positions.append(idx)

    if not cited_positions:
        return {
            "chunk_ids": retrieval_chunk_ids,
            "citations": [],
            "retrieval_chunk_ids": retrieval_chunk_ids,
            "citation_integrity": "missing",
        }

    seen_ids: set[int] = set()
    chunk_ids: list[int] = []
    citations: list[dict] = []
    for pos in cited_positions:
        chunk = chunks[pos]
        chunk_id = chunk.id
        if chunk_id in seen_ids:
            continue
        seen_ids.add(chunk_id)
        chunk_ids.append(chunk_id)
        document = getattr(chunk, "document", None)
        citations.append(
            {
                "citation_index": pos + 1,
                "chunk_id": chunk_id,
                "document_slug": getattr(document, "slug", None),
                "document_name": getattr(document, "name", None),
                "chunk_index": getattr(chunk, "chunk_index", None),
            }
        )

    integrity = "complete" if len(chunk_ids) == len(retrieval_chunk_ids) else "partial"
    return {
        "chunk_ids": chunk_ids,
        "citations": citations,
        "retrieval_chunk_ids": retrieval_chunk_ids,
        "citation_integrity": integrity,
    }


def _chunk_ids_from_citations(answer_text: str, retrieval: RetrievalResult) -> list[int]:
    """Backward-compatible helper retained for tests/callers."""
    return _extract_citation_payload(answer_text, retrieval)["chunk_ids"]


def _run_retrieval(
    session: ChatSession,
    content: str,
    user,
    response_mode: str | None = None,
) -> RetrievalResult:
    """
    Defensive wrapper around the RAG pipeline. Failures (embeddings, pgvector,
    OpenAI keys) must not bubble up as 500s — return an empty result so the
    chat still answers using base knowledge.

    History-aware query rewrite is applied BEFORE the path split so that
    follow-up questions carry conversational context into the vector search
    regardless of whether the session is in library or document-scoped mode.

    Skip rewrite for short standalone queries (< 5 words) — they are typically
    self-contained and the LLM call (10s timeout) would be wasteful.
    """
    # ── 1. Shared: history-aware query rewrite ────────────────────────────────
    # Only rewrite when the query contains anaphoric references (pronouns,
    # dangling subjects, elliptic follow-ups). Self-contained questions are
    # used as-is, skipping the LLM round-trip (~2-5s saved per message).
    retrieval_query = content
    history_qs = (
        session.messages
        .exclude(role=MessageRole.SYSTEM)
        .order_by("-created_at")[:4]
    )
    history = [
        {"role": str(m.role), "content": m.content or ""}
        for m in reversed(list(history_qs))
    ]
    content_words = len((content or "").split())
    if history and content_words >= 5 and _query_needs_context(content):
        try:
            retrieval_query = contextualize_query(content, history)
        except Exception as exc:
            logger.warning(
                "Query contextualization failed, using original (session=%s): %s",
                session.id, exc,
            )

    # ── 2. Path: general chat — library-wide pgvector search ─────────────────
    # Bounded to top_n chunks → O(1) RAM regardless of library size.
    allowed_docs = session.allowed_documents.all()
    if not allowed_docs.exists():
        try:
            return retrieve_from_library(user=user, query_text=retrieval_query)
        except Exception as exc:
            logger.exception("Library retrieval failed (session=%s): %s", session.id, exc)
            return RetrievalResult()

    # ── 3. Path: document-scoped chat ─────────────────────────────────────────
    try:
        result = retrieve_for_chat(
            user=user,
            query_text=retrieval_query,
            allowed_documents=allowed_docs,
            response_mode=response_mode,
            **_chat_retrieval_params(
                session,
                retrieval_query,
                response_mode=response_mode,
                doc_count_override=allowed_docs.count(),
            ),
        )
        if _workspace_session(session):
            try:
                result.recommended_documents = suggest_related_library_documents(
                    user=user,
                    query_text=content,
                    exclude_document_ids=list(allowed_docs.values_list("id", flat=True)),
                )
            except Exception as rec_exc:
                logger.warning(
                    "Recomendaciones de biblioteca omitidas (session=%s): %s",
                    session.id,
                    rec_exc,
                )
        return result
    except Exception as exc:
        logger.exception("Chat RAG pipeline failed (session=%s): %s", session.id, exc)
        return RetrievalResult()


def _compose_messages(
    session: ChatSession,
    content: str,
    retrieval: RetrievalResult,
    response_mode: str | None = None,
    *,
    max_history: int = MAX_HISTORY_MESSAGES,
) -> list[dict]:
    """Build the OpenAI messages list (system + context + history + user)."""
    system_text = (session.system_prompt or "").strip() or (
        "Eres Ecofilia, un asistente útil. Responde de forma clara y concisa."
    )

    # Adaptive system prompt: when no RAG context is available (greeting,
    # off-topic query, or library gap) and the stored prompt is the
    # context-dependent default, substitute a conversational fallback so the
    # LLM doesn't respond with "No tengo información disponible."
    if not retrieval.context_block and _RAG_CONTEXT_REQUIRED_MARKER in system_text:
        doc_count = session.allowed_documents.count()
        if doc_count == 1:
            single_doc = session.allowed_documents.first()
            doc_name = single_doc.name if single_doc else "el documento"
            system_text = _SINGLE_DOC_NO_CONTEXT_PROMPT.format(doc_name=doc_name)
        elif session.primary_document_id is not None:
            doc_name = session.primary_document.name if session.primary_document else "el documento"
            system_text = _SINGLE_DOC_NO_CONTEXT_PROMPT.format(doc_name=doc_name)
        else:
            system_text = _CONVERSATIONAL_FALLBACK_PROMPT

    base_messages: list[dict] = [
        {"role": str(MessageRole.SYSTEM), "content": system_text},
    ]

    if retrieval.context_block:
        doc_count = retrieval.diagnostics.get("documents_in_scope") or session.allowed_documents.count()
        query_type = retrieval.analysis.query_type if retrieval.analysis else None
        is_panorama = query_type in {QUERY_TYPE_PANORAMA, QUERY_TYPE_COMPARATIVE}

        if doc_count == 1:
            single_doc = session.allowed_documents.first()
            doc_label = f'del documento "{single_doc.name}"' if single_doc else "del documento"
            context_preamble = (
                f"El siguiente contexto proviene {doc_label}. "
                "Este es el único documento en esta sesión. Todas las preguntas del usuario son sobre este documento exclusivamente. "
                "Basate en este contexto para responder con precisión. "
                "Si la información solicitada no aparece en el contexto, indícalo claramente. "
                "No hagas referencia a otros documentos ni a información que no esté en este contexto. "
            )
        elif is_panorama:
            # For PANORAMA / COMPARATIVE queries each fragment comes from a
            # different document (country/entity). The LLM must SYNTHESIZE
            # across all fragments and compile a structured answer. It must NOT
            # fire the "No encontré evidencia documental" clause just because
            # the context doesn't contain a single pre-compiled list — that
            # clause is for truly absent information, not for distributed data
            # spread across per-country fragments.
            unique_docs = retrieval.diagnostics.get("unique_documents") or len(
                {c.document_id for c in (retrieval.chunks or [])}
            )
            context_preamble = (
                f"El siguiente contexto contiene {unique_docs} fragmento(s) de documentos distintos "
                "de la biblioteca de Ecofilia, cada uno correspondiente a un país o entidad diferente. "
                "Tu objetivo es SINTETIZAR y COMPILAR la información de todos los fragmentos "
                "para construir una respuesta de panorama regional. "
                "Lee cada fragmento e EXTRAE los datos relevantes para la consulta del usuario; "
                "luego PRESENTÁ una lista, tabla o resumen con lo que cada fragmento indica. "
                "Si un fragmento cubre un país pero no contiene el dato específico pedido, "
                "inclúyelo de todas formas con la nota 'dato no especificado en el fragmento'. "
                "NO escribas 'No encontré evidencia documental' cuando hay fragmentos en el contexto: "
                "esa frase está reservada para países o entidades sobre los que NO hay ningún fragmento. "
                "Si usás conocimiento propio (no documental), marcalo con [información general] "
                "y NO le adjuntés una cita [#N] a esa afirmación. "
            )
        else:
            context_preamble = (
                "El siguiente contexto proviene de documentos de la biblioteca de Ecofilia. "
                "Basá tu respuesta en la información de los fragmentos. "
                "Podés sintetizar y relacionar lo que dicen los fragmentos, pero no inventes datos "
                "que no estén respaldados por al menos un fragmento. "
                "Si para un país, entidad o dato específico no hay ningún fragmento con evidencia, "
                "indicalo brevemente: 'Sin evidencia documental sobre [X].' "
                "Si usás conocimiento propio (no documental), marcalo con [información general] "
                "y NO le adjuntés una cita [#N] a esa afirmación. "
            )
        base_messages.append(
            {
                "role": str(MessageRole.SYSTEM),
                "content": (
                    context_preamble
                    + build_citation_prompt(query_type=query_type)
                    + _build_coverage_instruction(session, retrieval)
                    + f"\n\n{retrieval.context_block}"
                ),
            }
        )

    history_qs = (
        session.messages.order_by("-created_at")
        .exclude(role=MessageRole.SYSTEM)[:max_history]
    )
    history_messages = [
        {"role": str(message.role), "content": message.content or ""}
        for message in reversed(list(history_qs))
    ]

    messages = base_messages + history_messages
    if response_mode:
        mode_instructions = {
            "tabla": (
                "Responde en formato tabla Markdown (GFM) cuando sea posible. "
                "Incluye encabezados claros y al menos 2 columnas. "
                "Si no hay datos suficientes para tabular, explicá brevemente por qué."
            ),
        }
        extra_instruction = mode_instructions.get(response_mode, "")
        messages.append(
            {
                "role": str(MessageRole.SYSTEM),
                "content": (
                    "El usuario seleccionó un modo de respuesta explícito. "
                    f"Prioriza este modo: '{response_mode}'."
                    + (f" {extra_instruction}" if extra_instruction else "")
                ),
            }
        )
    messages.append({"role": str(MessageRole.USER), "content": content})
    return messages


class ChatSessionPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"
    max_page_size = 100


class ChatCompletionFailed(APIException):
    """OpenAI u otro fallo al generar respuesta — 503 para que el cliente reciba JSON con detail."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "No se pudo generar la respuesta del asistente."
    default_code = "chat_completion_failed"


class ChatSessionViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = ChatSessionSerializer
    pagination_class = ChatSessionPagination

    def get_queryset(self):
        qs = ChatSession.objects.prefetch_related("allowed_documents")
        if self.request.user.is_staff:
            base = qs
        else:
            # Las suyas, salvo las de operaciones de una institución de la que
            # ya no es parte (las respuestas citan sus documentos).
            from apps.user.access import institution_boundary_q

            base = qs.filter(owner=self.request.user).filter(
                institution_boundary_q(self.request.user, "project__organization")
            )

        # Listado: no devolver sesiones vacías salvo ?include_empty=true (evita ruido y carga innecesaria).
        if getattr(self, "action", None) == "list":
            raw = (self.request.query_params.get("include_empty") or "").lower()
            if raw not in ("1", "true", "yes"):
                base = base.filter(
                    Exists(
                        ChatMessage.objects.filter(session_id=OuterRef("pk"))
                    )
                )
        return base.order_by("-updated_at", "-created_at", "-id")

    def get_serializer_class(self):
        if self.action == "create":
            return ChatSessionCreateSerializer
        return ChatSessionSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        session = serializer.save(owner=request.user)
        output = ChatSessionSerializer(
            session, context=self.get_serializer_context()
        )
        headers = self.get_success_headers(output.data)
        return Response(output.data, status=status.HTTP_201_CREATED, headers=headers)


class ChatMessageViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    viewsets.GenericViewSet,
):
    permission_classes = [IsAuthenticated]
    serializer_class = ChatMessageSerializer

    def get_queryset(self):
        qs = ChatMessage.objects.select_related("session", "session__owner").order_by(
            "created_at"
        )
        if not self.request.user.is_staff:
            from apps.user.access import institution_boundary_q

            qs = qs.filter(session__owner=self.request.user).filter(
                institution_boundary_q(self.request.user, "session__project__organization")
            )

        session_id = self.request.query_params.get("session")
        if session_id:
            qs = qs.filter(session_id=session_id)
        return qs

    def list(self, request, *args, **kwargs):
        session_id = request.query_params.get("session")
        if not session_id:
            return Response(
                {"detail": "El parámetro 'session' es obligatorio."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        queryset = self.filter_queryset(self.get_queryset())
        messages = list(queryset)

        all_chunk_ids: list[int] = []
        for message in messages:
            all_chunk_ids.extend(message.chunk_ids or [])

        serializer_context = self.get_serializer_context()
        serializer_context["include_chunk_content"] = False
        serializer_context["chunks_by_id"] = prefetch_chunks_by_id(
            all_chunk_ids,
            include_content=False,
        )
        serializer = self.get_serializer(
            messages,
            many=True,
            context=serializer_context,
        )
        return Response(serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = ChatMessageCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        session = serializer.validated_data["session"]
        content = serializer.validated_data["content"]
        document_slugs = serializer.validated_data.get("document_slugs")
        response_mode = serializer.validated_data.get("response_mode")

        _sync_session_documents_for_request(session, document_slugs)
        retrieval = _run_retrieval(session, content, request.user, response_mode=response_mode)
        fanout_result = maybe_fanout(
            retrieval,
            user=request.user,
            query_text=content,
            allowed_documents=session.allowed_documents.all(),
            response_mode=response_mode,
        )
        messages = _compose_messages(
            session,
            content,
            retrieval,
            response_mode=response_mode,
        )

        with transaction.atomic():
            user_message = ChatMessage.objects.create(
                session=session,
                role=MessageRole.USER,
                content=content,
            )

            if fanout_result is not None:
                # Phase 4: per-document map-reduce answer (extract_per_entity).
                apply_fanout(retrieval, fanout_result)
                answer_text, usage = fanout_result.answer, fanout_result.usage
            else:
                answer_text, usage = _generate_answer_or_raise(messages, session)

            metadata: dict = {"usage": usage}
            # Traceability: which model actually produced this answer. Fan-out
            # answers come from the map tier; otherwise the resolved chat model.
            if fanout_result is not None:
                metadata["model"] = (fanout_result.diagnostics or {}).get(
                    "fanout_map_model"
                ) or effective_chat_model(session.model)
            else:
                metadata["model"] = effective_chat_model(session.model)
            if retrieval.diagnostics:
                metadata["rag_diagnostics"] = retrieval.diagnostics
            if retrieval.analysis is not None:
                metadata["query_analysis"] = retrieval.analysis.to_dict()
            if response_mode:
                metadata["response_mode"] = response_mode
            if retrieval.recommended_documents:
                metadata["recommended_documents"] = retrieval.recommended_documents

            citation_payload = _extract_citation_payload(answer_text, retrieval)
            metadata["citations"] = citation_payload["citations"]
            metadata["retrieval_chunk_ids"] = citation_payload["retrieval_chunk_ids"]
            metadata["citation_integrity"] = citation_payload["citation_integrity"]

            assistant_message = ChatMessage.objects.create(
                session=session,
                role=MessageRole.ASSISTANT,
                content=answer_text,
                chunk_ids=citation_payload["chunk_ids"],
                metadata=metadata,
            )
            touch_chat_session_activity(session.pk)

        response_payload = {
            "user_message": ChatMessageSerializer(user_message).data,
            "assistant_message": ChatMessageSerializer(assistant_message).data,
        }
        return Response(response_payload, status=status.HTTP_201_CREATED)


def _build_chat_messages(
    session: ChatSession,
    content: str,
    user,
    *,
    response_mode: str | None = None,
    max_history: int = MAX_HISTORY_MESSAGES,
):
    """
    Shared helper used by the streaming endpoint.
    Returns (openai_messages, retrieval_result).
    """
    retrieval = _run_retrieval(session, content, user, response_mode=response_mode)
    messages = _compose_messages(
        session,
        content,
        retrieval,
        response_mode=response_mode,
        max_history=max_history,
    )
    return messages, retrieval


def _generate_answer_or_raise(messages, session):
    """Single-pass generation with friendly error mapping (provider-agnostic)."""
    try:
        return client_openia.generate_chat_completion(
            messages,
            model=effective_chat_model(session.model),
            temperature=session.temperature,
            timeout=90,
        )
    except Exception as exc:  # pragma: no cover - network failure
        error_msg = str(exc)
        logger.exception("Error al generar respuesta: %s", error_msg)
        low = error_msg.lower()
        if "api_key" in low or "authentication" in low:
            raise ChatCompletionFailed(
                detail="Error de autenticación con el proveedor de IA. Verifica la configuración de la API key.",
            ) from exc
        if "rate limit" in low or "quota" in low:
            raise ChatCompletionFailed(
                detail="Límite de tasa excedido. Por favor, intenta de nuevo en unos momentos.",
            ) from exc
        if "model" in low:
            raise ChatCompletionFailed(
                detail=f"Error con el modelo de IA: {error_msg}",
            ) from exc
        raise ChatCompletionFailed(
            detail=f"No fue posible generar la respuesta en este momento: {error_msg}",
        ) from exc


def _sync_session_documents_for_request(
    session: ChatSession, document_slugs: list[str] | None
):
    """
    Optionally update session scope before retrieval so each message can use
    the latest selected sources from the workspace UI.
    """
    if document_slugs is None:
        return
    docs = Document.objects.filter(slug__in=document_slugs)
    session.allowed_documents.set(docs)


class ChatMessageStreamView(APIView):
    """
    POST /chat/messages/stream/

    SSE endpoint — streams the assistant reply token-by-token.

    Event types sent:
      • {"type": "user_message", "message": {...}}   — the persisted user ChatMessage
      • {"type": "chunk",        "content": "..."}   — incremental text fragment
      • {"type": "done",         "message": {...}}   — the persisted assistant ChatMessage
      • {"type": "error",        "detail":  "..."}   — on failure (user message is rolled back)
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = ChatMessageCreateSerializer(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        session = serializer.validated_data["session"]
        content = serializer.validated_data["content"]
        document_slugs = serializer.validated_data.get("document_slugs")
        response_mode = serializer.validated_data.get("response_mode")

        _sync_session_documents_for_request(session, document_slugs)

        user_message = ChatMessage.objects.create(
            session=session,
            role=MessageRole.USER,
            content=content,
        )

        def _event(payload: dict) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def stream():
            yield _event(
                {
                    "type": "user_message",
                    "message": ChatMessageSerializer(user_message).data,
                }
            )
            if os.environ.get("RAG_STREAM_EARLY_EVENT_ENABLED", "1").lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                yield _event(
                    {
                        "type": "status",
                        "phase": "retrieval",
                        "detail": "Preparando contexto documental",
                    }
                )

            collected: list[str] = []
            try:
                messages, retrieval = _build_chat_messages(
                    session,
                    content,
                    request.user,
                    response_mode=response_mode,
                )
                fanout_result = maybe_fanout(
                    retrieval,
                    user=request.user,
                    query_text=content,
                    allowed_documents=session.allowed_documents.all(),
                    response_mode=response_mode,
                )
                if fanout_result is not None:
                    # Phase 4: per-document map-reduce (extract_per_entity).
                    apply_fanout(retrieval, fanout_result)
                    answer_text = fanout_result.answer
                    yield _event({"type": "chunk", "content": answer_text})
                else:
                    for text_chunk in client_openia.generate_chat_completion_stream(
                        messages,
                        model=effective_chat_model(session.model),
                        temperature=session.temperature,
                        timeout=90,
                    ):
                        collected.append(text_chunk)
                        yield _event({"type": "chunk", "content": text_chunk})

                    answer_text = "".join(collected)
                metadata: dict = {}
                if fanout_result is not None:
                    metadata["model"] = (fanout_result.diagnostics or {}).get(
                        "fanout_map_model"
                    ) or effective_chat_model(session.model)
                else:
                    metadata["model"] = effective_chat_model(session.model)
                if retrieval.diagnostics:
                    metadata["rag_diagnostics"] = retrieval.diagnostics
                if retrieval.analysis is not None:
                    metadata["query_analysis"] = retrieval.analysis.to_dict()
                if response_mode:
                    metadata["response_mode"] = response_mode
                if retrieval.recommended_documents:
                    metadata["recommended_documents"] = retrieval.recommended_documents
                citation_payload = _extract_citation_payload(answer_text, retrieval)
                metadata["citations"] = citation_payload["citations"]
                metadata["retrieval_chunk_ids"] = citation_payload["retrieval_chunk_ids"]
                metadata["citation_integrity"] = citation_payload["citation_integrity"]

                assistant_message = ChatMessage.objects.create(
                    session=session,
                    role=MessageRole.ASSISTANT,
                    content=answer_text,
                    chunk_ids=citation_payload["chunk_ids"],
                    metadata=metadata,
                )
                touch_chat_session_activity(session.pk)
                yield _event(
                    {
                        "type": "done",
                        "message": ChatMessageSerializer(assistant_message).data,
                    }
                )

            except Exception as exc:
                logger.exception(
                    "Streaming chat completion failed (session=%s): %s", session.id, exc
                )
                user_message.delete()
                yield _event({"type": "error", "detail": str(exc)})

        response = StreamingHttpResponse(stream(), content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response
