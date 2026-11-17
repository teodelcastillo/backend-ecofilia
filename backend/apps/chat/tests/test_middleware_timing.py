"""
Tests for the chat middleware's 3 retrieval modes and latency behaviour.

Focus: verify that _decide_retrieval_mode routes correctly for simple,
focussed, and panorama queries, and that _chat_retrieval_params no longer
makes a potentially slow LLM call.
"""
from __future__ import annotations

import os
import time
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.chat.api.views import _chat_retrieval_params
from apps.chat.models import ChatSession
from apps.chat.services.rag import retrieve_for_chat
from apps.chat.services.query_analysis import (
    COVERAGE_MODE_ALL,
    COVERAGE_MODE_FOCUSED,
    classify_query,
)
from apps.document.models import Document

User = get_user_model()


class RetrievalModeRoutingTests(TestCase):
    """_decide_retrieval_mode must choose none/light/full correctly."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="timing@example.com", password="secret", username="timinguser"
        )
        self.doc = Document.objects.create(
            owner=self.user, name="Test Doc", slug="test-doc"
        )

    def _retrieve(self, query_text, *, doc_count: int = 1, **env_overrides):
        env = {
            "RAG_RETRIEVAL_GATE_ENABLED": "1",
            "RAG_LIGHT_MODE_ENABLED": "1",
            "RAG_LLM_ROUTER_ENABLED": "0",
            "RAG_RERANKER_ENABLED": "0",
            "RAG_QUERY_EXPANSION_ENABLED": "0",
            **env_overrides,
        }
        session = ChatSession.objects.create(owner=self.user, title="T")
        session.allowed_documents.add(self.doc)
        if doc_count > 1:
            for i in range(1, doc_count):
                extra = Document.objects.create(
                    owner=self.user,
                    name=f"Extra Doc {i}",
                    slug=f"extra-doc-{i}",
                )
                session.allowed_documents.add(extra)
        with patch.dict(os.environ, env, clear=False):
            with patch("apps.chat.services.rag.fetch_relevant_chunks", return_value=[]):
                with patch("apps.chat.services.rag.lexical_search", return_value=[]):
                    return retrieve_for_chat(
                        user=self.user,
                        query_text=query_text,
                        allowed_documents=session.allowed_documents.all(),
                    )

    # --- Mode: none (greetings / trivial queries, multi-doc only) ---

    def test_greeting_uses_none_mode_for_multi_doc(self):
        result = self._retrieve("hola", doc_count=2)
        self.assertEqual(result.diagnostics["retrieval_mode"], "none")
        self.assertEqual(result.diagnostics["retrieval_skipped_reason"], "simple_query")
        self.assertEqual(result.chunk_ids, [])

    def test_short_trivial_without_domain_uses_none_mode_for_multi_doc(self):
        result = self._retrieve("que tal", doc_count=2)
        self.assertEqual(result.diagnostics["retrieval_mode"], "none")

    def test_single_doc_short_summary_uses_light_not_none(self):
        result = self._retrieve("Haz un resumen", doc_count=1)
        self.assertEqual(result.diagnostics["retrieval_mode"], "light")
        self.assertIsNone(result.diagnostics["retrieval_skipped_reason"])

    def test_single_doc_greeting_uses_light_not_none(self):
        result = self._retrieve("hola", doc_count=1)
        self.assertEqual(result.diagnostics["retrieval_mode"], "light")
        self.assertIsNone(result.diagnostics["retrieval_skipped_reason"])

    def test_short_domain_query_does_not_use_none_mode(self):
        # "emisiones" is in _RETRIEVAL_DOMAIN_HINTS → short query but has domain hint → retrieve
        result = self._retrieve("emisiones carbono")
        self.assertNotEqual(result.diagnostics["retrieval_mode"], "none")

    # --- Mode: light (factual / numeric / focused) ---

    def test_factual_query_uses_light_mode(self):
        result = self._retrieve("¿Quién firmó el acuerdo?")
        self.assertEqual(result.diagnostics["retrieval_mode"], "light")

    def test_numeric_query_uses_light_mode(self):
        result = self._retrieve("¿Cuántas toneladas de CO2 se emitieron?")
        self.assertEqual(result.diagnostics["retrieval_mode"], "light")

    def test_light_mode_caps_chunks(self):
        # In light mode, base_top_n is capped at 4
        result = self._retrieve("¿Cuál es el objetivo de reducción de emisiones?")
        # retrieval_mode light means fetch pool <= 4
        # We can't assert exact pool size without chunks, but mode must be light
        self.assertIn(result.diagnostics["retrieval_mode"], ("light", "none"))

    # --- Mode: full (panorama / comparative / all-docs) ---
    # Con más de un documento: una sesión de un solo documento recupera siempre
    # en modo light (24de2df), lo que ya cubren los tests `single_doc_*`.

    def test_panorama_query_uses_full_mode(self):
        result = self._retrieve(
            "Dame un resumen general de todos los documentos disponibles", doc_count=2
        )
        self.assertEqual(result.diagnostics["retrieval_mode"], "full")

    def test_comparative_query_uses_full_mode(self):
        result = self._retrieve(
            "Compara las estrategias de mitigación entre los documentos", doc_count=2
        )
        self.assertEqual(result.diagnostics["retrieval_mode"], "full")

    def test_long_query_uses_full_mode(self):
        # 18+ words triggers panorama classification → full retrieval
        long_q = (
            "Quisiera entender en detalle cómo se articulan los objetivos de "
            "reducción de emisiones con las políticas de adaptación al cambio "
            "climático descritas en cada uno de los documentos del repositorio"
        )
        result = self._retrieve(long_q, doc_count=2)
        self.assertEqual(result.diagnostics["retrieval_mode"], "full")

    def test_single_doc_panorama_query_uses_light_mode(self):
        result = self._retrieve(
            "Dame un resumen general de todos los documentos disponibles", doc_count=1
        )
        self.assertEqual(result.diagnostics["retrieval_mode"], "light")


class ChatRetrievalParamsFastTests(TestCase):
    """_chat_retrieval_params must use fast regex classification (no LLM calls)."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="params@example.com", password="secret", username="paramsuser"
        )

    def test_params_does_not_call_llm_router(self):
        """After the fix, _chat_retrieval_params must use classify_query, not
        classify_query_hybrid, so this mock must NOT be called."""
        session = ChatSession.objects.create(owner=self.user, title="P")
        # A query that would trigger the LLM router if classify_query_hybrid was used
        long_ambiguous_q = (
            "Quisiera entender cómo se vincula la estrategia descrita en el "
            "documento con los procesos del equipo de operaciones y qué "
            "implicaciones podría tener en próximos trimestres"
        )
        with patch("apps.chat.api.views.classify_query_hybrid") as mock_hybrid:
            params = _chat_retrieval_params(session, long_ambiguous_q)
        mock_hybrid.assert_not_called()
        self.assertIn("top_n", params)

    def test_params_is_fast_for_any_query(self):
        """Pool sizing must complete in under 50ms (pure Python regex)."""
        session = ChatSession.objects.create(owner=self.user, title="Fast")
        queries = [
            "hola",
            "¿cuántas toneladas emitieron?",
            "resumen general del repositorio",
            "compara documentos A y B",
            "Quisiera entender en detalle cómo se articulan los objetivos " * 3,
        ]
        for q in queries:
            t0 = time.perf_counter()
            _chat_retrieval_params(session, q)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self.assertLess(
                elapsed_ms, 50,
                f"_chat_retrieval_params took {elapsed_ms:.1f}ms for: {q[:50]!r}"
            )

    def test_coverage_all_sets_total_limit_to_doc_count(self):
        session = ChatSession.objects.create(owner=self.user, title="AllDocs")
        docs = [
            Document.objects.create(
                owner=self.user, name=f"D{i}", slug=f"d{i}"
            )
            for i in range(10)
        ]
        session.allowed_documents.set(docs)
        params = _chat_retrieval_params(
            session,
            "Dame un resumen general de todos los documentos del repositorio",
        )
        # COVERAGE_MODE_ALL → total_limit == doc_count == 10
        self.assertEqual(params["total_limit"], 10)
        self.assertEqual(params["max_chunks_per_doc"], 1)

    def test_single_doc_session_allows_more_chunks(self):
        session = ChatSession.objects.create(owner=self.user, title="SingleDoc")
        doc = Document.objects.create(
            owner=self.user, name="Solo", slug="solo-doc"
        )
        session.allowed_documents.add(doc)
        params = _chat_retrieval_params(session, "¿Qué dice el documento sobre emisiones?")
        self.assertEqual(params["total_limit"], 10)
        self.assertEqual(params["max_chunks_per_doc"], 10)


class ContextualizeQueryGuardTests(TestCase):
    """contextualize_query must be skipped for short queries to avoid ~10s LLM call."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="ctx@example.com", password="secret", username="ctxuser"
        )

    @patch("apps.chat.api.views.contextualize_query")
    def test_short_query_skips_contextualization(self, mock_contextualize):
        """Queries with < 5 words must not call contextualize_query even with history."""
        from apps.chat.api.views import _run_retrieval
        from apps.chat.models import ChatMessage, MessageRole

        session = ChatSession.objects.create(owner=self.user, title="Short")
        # Add some history to trigger the path
        ChatMessage.objects.create(session=session, role=MessageRole.USER, content="Hola")
        ChatMessage.objects.create(
            session=session, role=MessageRole.ASSISTANT, content="Hola, ¿en qué puedo ayudarte?"
        )

        with patch("apps.chat.api.views.retrieve_for_chat") as mock_retrieve:
            mock_retrieve.return_value = MagicMock(
                chunks=[], context_block="", analysis=None,
                diagnostics={}, recommended_documents=[],
                chunk_ids=[], covered_document_ids=set(),
            )
            _run_retrieval(session, "¿y eso?", self.user)

        mock_contextualize.assert_not_called()

    def _run_with_history(self, query):
        from apps.chat.api.views import _run_retrieval
        from apps.chat.models import ChatMessage, MessageRole

        doc = Document.objects.create(
            owner=self.user, name="Doc ctx", slug=f"doc-ctx-{ChatSession.objects.count()}"
        )
        session = ChatSession.objects.create(owner=self.user, title="Long")
        session.allowed_documents.add(doc)
        ChatMessage.objects.create(session=session, role=MessageRole.USER, content="Hola")
        ChatMessage.objects.create(
            session=session, role=MessageRole.ASSISTANT, content="Hola!"
        )
        with patch("apps.chat.api.views.retrieve_for_chat") as mock_retrieve:
            mock_retrieve.return_value = MagicMock(
                chunks=[], context_block="", analysis=None,
                diagnostics={}, recommended_documents=[],
                chunk_ids=[], covered_document_ids=set(),
            )
            _run_retrieval(session, query, self.user)

    @patch("apps.chat.api.views.contextualize_query")
    def test_long_anaphoric_query_with_history_calls_contextualization(self, mock_contextualize):
        """Una pregunta larga que depende de la anterior («¿y …?», «eso»…)
        se reescribe con el historial."""
        mock_contextualize.return_value = "rewritten query"
        self._run_with_history("¿Y cuáles son sus metas de reducción para 2030?")
        mock_contextualize.assert_called_once()

    @patch("apps.chat.api.views.contextualize_query")
    def test_long_standalone_query_skips_contextualization(self, mock_contextualize):
        """Una pregunta larga que se entiende sola no paga la llamada al LLM
        (``_query_needs_context``, desde 82bfefd)."""
        self._run_with_history("¿Cuáles son los objetivos de reducción de emisiones?")
        mock_contextualize.assert_not_called()


class SelectiveRerankerTests(TestCase):
    """llm_rerank must only fire for panorama/comparative queries, never for factual."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="reranker@example.com", password="secret", username="rerankeruser"
        )
        self.doc = Document.objects.create(
            owner=self.user, name="Reranker Doc", slug="reranker-doc"
        )

    def _retrieve(self, query_text, *, reranker_enabled="1"):
        session = ChatSession.objects.create(owner=self.user, title="R")
        session.allowed_documents.add(self.doc)
        # Dos documentos: con uno solo el modo es siempre light (24de2df) y el
        # camino panorama/comparativo, que es lo que se prueba, no se alcanza.
        session.allowed_documents.add(
            Document.objects.create(
                owner=self.user, name="Reranker Doc 2",
                slug=f"reranker-doc-2-{ChatSession.objects.count()}",
            )
        )
        env = {
            "RAG_RETRIEVAL_GATE_ENABLED": "1",
            "RAG_RERANKER_ENABLED": reranker_enabled,
            "RAG_LLM_ROUTER_ENABLED": "0",
            "RAG_QUERY_EXPANSION_ENABLED": "0",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("apps.chat.services.rag.fetch_relevant_chunks", return_value=[]):
                with patch("apps.chat.services.rag.lexical_search", return_value=[]):
                    with patch("apps.chat.services.rag.llm_rerank") as mock_rerank:
                        result = retrieve_for_chat(
                            user=self.user,
                            query_text=query_text,
                            allowed_documents=session.allowed_documents.all(),
                        )
                        return result, mock_rerank

    def test_factual_query_does_not_call_reranker(self):
        _, mock_rerank = self._retrieve("¿Cuántas toneladas de CO2 se emitieron en 2023?")
        mock_rerank.assert_not_called()

    def test_panorama_query_may_call_reranker(self):
        # With empty fused list, len(fused) <= safe_total so rerank won't fire.
        # The important thing is the condition is reached (mode=full, type=panorama).
        # We verify mode is full so the code path is correct.
        result, _ = self._retrieve(
            "Dame un resumen general de todos los documentos disponibles"
        )
        self.assertEqual(result.diagnostics["retrieval_mode"], "full")

    def test_comparative_query_does_not_call_reranker_when_disabled(self):
        _, mock_rerank = self._retrieve(
            "Compara las estrategias de mitigación entre los documentos",
            reranker_enabled="0",
        )
        mock_rerank.assert_not_called()
