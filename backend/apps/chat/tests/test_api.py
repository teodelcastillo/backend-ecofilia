from unittest.mock import patch
import os
import uuid

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.chat.api.views import (
    _CONVERSATIONAL_FALLBACK_PROMPT,
    _chat_retrieval_params,
    _chunk_ids_from_citations,
    _compose_messages,
    _extract_citation_payload,
)
from apps.chat.models import ChatSession
from apps.chat.services.rag import RetrievalResult, retrieve_for_chat
from apps.chat.services.query_analysis import COVERAGE_MODE_ALL, classify_query
from apps.document.models import Document, SmartChunk

User = get_user_model()


class ChatAPITestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com", password="secret123", username="owner"
        )
        self.other_user = User.objects.create_user(
            email="other@example.com", password="secret123", username="other"
        )
        self.document = Document.objects.create(
            owner=self.user, name="Doc 1", slug="doc-1", is_public=False
        )
        self.public_document = Document.objects.create(
            owner=self.other_user, name="Public Doc", slug="public-doc", is_public=True
        )
        self.client.force_authenticate(self.user)

    def test_create_session_with_owned_document(self):
        url = reverse("chat-session-list")
        payload = {
            "title": "Investigación",
            "document_slugs": ["doc-1"],
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["document_slugs"], ["doc-1"])

    def test_create_session_rejects_forbidden_documents(self):
        url = reverse("chat-session-list")
        payload = {
            "title": "No permitido",
            "document_slugs": ["non-existent"],
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_session_rejects_too_many_documents(self):
        run_id = uuid.uuid4().hex[:8]
        docs = [
            Document.objects.create(
                owner=self.user,
                name=f"TooMany Doc {i}",
                slug=f"too-{run_id}-{i}",
            )
            for i in range(25)
        ]
        url = reverse("chat-session-list")
        payload = {
            "title": "Demasiado amplia",
            "document_slugs": [doc.slug for doc in docs],
        }

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("máximo", str(response.data))

    def test_repository_panorama_query_uses_all_docs_policy(self):
        analysis = classify_query(
            "Necesito saber en rasgos generales de que trata este repositorio"
        )
        self.assertEqual(analysis.coverage_mode, COVERAGE_MODE_ALL)

        session = ChatSession.objects.create(
            owner=self.user,
            title="Repositorio",
        )
        docs = [
            Document.objects.create(
                owner=self.user,
                name=f"Repo Doc {i}",
                slug=f"repo-doc-{i}",
            )
            for i in range(17)
        ]
        session.allowed_documents.set(docs)

        params = _chat_retrieval_params(
            session,
            "Necesito saber en rasgos generales de que trata este repositorio",
        )
        self.assertEqual(params["total_limit"], 17)
        self.assertEqual(params["top_n"], 17)
        self.assertEqual(params["max_chunks_per_doc"], 1)

    @patch("apps.chat.services.rag.lexical_search")
    @patch("apps.chat.services.rag.fetch_relevant_chunks")
    @patch("apps.document.utils.client_openia.generate_chat_completion")
    def test_create_message_returns_assistant_response(
        self, mock_completion, mock_fetch_chunks, mock_lexical
    ):
        mock_completion.return_value = ("Respuesta generada", {"total_tokens": 10})

        chunk = SmartChunk.objects.create(
            document=self.document,
            chunk_index=0,
            content="Información relevante sobre emisiones",
            token_count=5,
        )
        mock_fetch_chunks.return_value = [chunk]
        mock_lexical.return_value = []

        session = ChatSession.objects.create(
            owner=self.user,
            title="Sesión",
        )
        session.allowed_documents.add(self.document)

        url = reverse("chat-message-list")
        payload = {"session": session.id, "content": "¿Qué dice el documento sobre emisiones?"}

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("assistant_message", response.data)
        self.assertEqual(
            response.data["assistant_message"]["content"], "Respuesta generada"
        )

    def test_list_messages_requires_session_param(self):
        session = ChatSession.objects.create(owner=self.user, title="Sesión")
        url = reverse("chat-message-list")
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_messages_filters_by_session(self):
        session_a = ChatSession.objects.create(owner=self.user, title="A")
        session_b = ChatSession.objects.create(owner=self.user, title="B")
        from apps.chat.models import ChatMessage, MessageRole

        ChatMessage.objects.create(
            session=session_a,
            role=MessageRole.USER,
            content="Hola A",
        )
        ChatMessage.objects.create(
            session=session_b,
            role=MessageRole.USER,
            content="Hola B",
        )

        url = reverse("chat-message-list")
        response = self.client.get(url, {"session": session_a.id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["content"], "Hola A")
        self.assertEqual(response.data[0]["session"], session_a.id)

    def test_chunk_ids_from_citations_keeps_only_cited_chunks(self):
        retrieval = RetrievalResult()
        retrieval.chunks = [type("Chunk", (), {"id": 11})(), type("Chunk", (), {"id": 22})()]
        ids = _chunk_ids_from_citations("Respuesta [#2] con cita puntual.", retrieval)
        self.assertEqual(ids, [22])

    def test_chunk_ids_from_citations_falls_back_when_no_citations(self):
        retrieval = RetrievalResult()
        retrieval.chunks = [type("Chunk", (), {"id": 11})(), type("Chunk", (), {"id": 22})()]
        ids = _chunk_ids_from_citations("Respuesta sin marcadores de cita.", retrieval)
        self.assertEqual(ids, [11, 22])

    def test_extract_citation_payload_contains_mapping_metadata(self):
        chunk_a = type(
            "Chunk",
            (),
            {
                "id": 11,
                "chunk_index": 2,
                "document": type("Doc", (), {"slug": "doc-a", "name": "Doc A"})(),
            },
        )()
        chunk_b = type(
            "Chunk",
            (),
            {
                "id": 22,
                "chunk_index": 4,
                "document": type("Doc", (), {"slug": "doc-b", "name": "Doc B"})(),
            },
        )()
        retrieval = RetrievalResult(chunks=[chunk_a, chunk_b])
        payload = _extract_citation_payload("Texto con cita [#2].", retrieval)

        self.assertEqual(payload["chunk_ids"], [22])
        self.assertEqual(payload["retrieval_chunk_ids"], [11, 22])
        self.assertEqual(payload["citation_integrity"], "partial")
        self.assertEqual(payload["citations"][0]["citation_index"], 2)
        self.assertEqual(payload["citations"][0]["chunk_id"], 22)

    @patch("apps.chat.services.rag.lexical_search")
    @patch("apps.chat.services.rag.fetch_relevant_chunks")
    @patch("apps.document.utils.client_openia.generate_chat_completion")
    def test_create_message_without_evidence_returns_empty_chunk_ids(
        self, mock_completion, mock_fetch_chunks, mock_lexical
    ):
        mock_completion.return_value = ("Respuesta general sin evidencia documental.", {"total_tokens": 10})
        mock_fetch_chunks.return_value = []
        mock_lexical.return_value = []

        session = ChatSession.objects.create(owner=self.user, title="Global")
        url = reverse("chat-message-list")
        payload = {"session": session.id, "content": "¿Qué dice el Acuerdo de París?"}

        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["assistant_message"]["chunk_ids"], [])

    @patch("apps.chat.services.rag.classify_query_hybrid")
    @patch("apps.chat.services.rag.lexical_search")
    @patch("apps.chat.services.rag.fetch_relevant_chunks")
    @patch("apps.document.utils.client_openia.generate_chat_completion")
    def test_create_message_persists_citation_metadata(
        self, mock_completion, mock_fetch_chunks, mock_lexical, mock_classify
    ):
        from apps.chat.services.query_analysis import (
            QueryAnalysis, QUERY_TYPE_FACTUAL, COVERAGE_MODE_FOCUSED,
            CLASSIFIER_CONFIDENCE_HIGH,
        )
        controlled_analysis = QueryAnalysis(
            raw_text="¿Qué dice el documento sobre emisiones de carbono?",
            normalized="que dice el documento sobre emisiones de carbono",
        )
        controlled_analysis.query_type = QUERY_TYPE_FACTUAL
        controlled_analysis.coverage_mode = COVERAGE_MODE_FOCUSED
        controlled_analysis.classifier_confidence = CLASSIFIER_CONFIDENCE_HIGH
        mock_classify.return_value = controlled_analysis

        mock_completion.return_value = ("Respuesta citada [#1].", {"total_tokens": 12})
        chunk = SmartChunk.objects.create(
            document=self.document,
            chunk_index=0,
            content="Evidencia importante sobre emisiones de carbono",
            token_count=6,
        )
        mock_fetch_chunks.return_value = [chunk]
        mock_lexical.return_value = []

        session = ChatSession.objects.create(owner=self.user, title="Sesión citada")
        session.allowed_documents.add(self.document)

        url = reverse("chat-message-list")
        env = {"RAG_RETRIEVAL_GATE_ENABLED": "1", "RAG_LIGHT_MODE_ENABLED": "1"}
        with patch.dict(os.environ, env, clear=False):
            response = self.client.post(
                url,
                {"session": session.id, "content": "¿Qué dice el documento sobre emisiones de carbono?"},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        metadata = response.data["assistant_message"]["metadata"]
        self.assertIn("citations", metadata)
        self.assertIn("retrieval_chunk_ids", metadata)
        self.assertIn("citation_integrity", metadata)
        self.assertEqual(metadata["citation_integrity"], "complete")

    def test_retrieve_for_chat_uses_none_mode_for_simple_query_multi_doc(self):
        session = ChatSession.objects.create(owner=self.user, title="Simple")
        session.allowed_documents.add(self.document)
        second = Document.objects.create(
            owner=self.user, name="Doc 2", slug="doc-2", is_public=False
        )
        session.allowed_documents.add(second)
        with patch.dict(os.environ, {"RAG_RETRIEVAL_GATE_ENABLED": "1"}, clear=False):
            result = retrieve_for_chat(
                user=self.user,
                query_text="hola",
                allowed_documents=session.allowed_documents.all(),
            )
        self.assertEqual(result.diagnostics.get("retrieval_mode"), "none")
        self.assertEqual(result.diagnostics.get("retrieval_skipped_reason"), "simple_query")
        self.assertEqual(result.chunk_ids, [])

    def test_retrieve_for_chat_single_doc_does_not_skip_simple_query(self):
        session = ChatSession.objects.create(owner=self.user, title="Single")
        session.allowed_documents.add(self.document)
        with patch.dict(os.environ, {"RAG_RETRIEVAL_GATE_ENABLED": "1"}, clear=False):
            with patch("apps.chat.services.rag.fetch_relevant_chunks", return_value=[]):
                with patch("apps.chat.services.rag.lexical_search", return_value=[]):
                    result = retrieve_for_chat(
                        user=self.user,
                        query_text="Haz un resumen",
                        allowed_documents=session.allowed_documents.all(),
                    )
        self.assertEqual(result.diagnostics.get("retrieval_mode"), "light")
        self.assertIsNone(result.diagnostics.get("retrieval_skipped_reason"))

    def test_compose_messages_single_doc_uses_document_fallback_not_library(self):
        session = ChatSession.objects.create(
            owner=self.user,
            title="Doc chat",
            system_prompt=(
                "Eres un asistente especializado en aprovechar el contexto entregado. "
                "Responde únicamente con la información disponible y menciona la fuente."
            ),
        )
        session.allowed_documents.add(self.document)
        retrieval = RetrievalResult()
        messages = _compose_messages(session, "Resúmelo", retrieval)
        system_content = messages[0]["content"]
        self.assertIn(self.document.name, system_content)
        # Lo que no tiene que aparecer es la sugerencia de la biblioteca
        # ("sugiérele que seleccione documentos"). El prompt de un documento
        # nombra "seleccione" para prohibirlo: "No pidas … que seleccione".
        self.assertNotIn("sugiérele que seleccione", system_content.lower())
        self.assertIn("no pidas al usuario que cargue", system_content.lower())
        self.assertNotEqual(system_content, _CONVERSATIONAL_FALLBACK_PROMPT)

    def test_document_chat_session_post_persists_custom_system_prompt(self):
        url = reverse("document-chat-session", kwargs={"slug": self.document.slug})
        custom_prompt = "Prompt personalizado para análisis documental."
        response = self.client.post(
            url,
            {
                "title": "Mi chat",
                "system_prompt": custom_prompt,
                "model": "gpt-4o-mini",
                "temperature": 0.2,
                "language": "es",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["system_prompt"], custom_prompt)
        self.assertEqual(response.data["title"], "Mi chat")
        self.assertEqual(response.data["document_slugs"], [self.document.slug])
        self.assertEqual(response.data["primary_document_slug"], self.document.slug)

    def test_retrieve_for_chat_marks_timeout_when_budget_exceeded(self):
        session = ChatSession.objects.create(owner=self.user, title="Budget")
        session.allowed_documents.add(self.document)
        with patch.dict(
            os.environ,
            {
                "RAG_RETRIEVAL_GATE_ENABLED": "1",
                "RAG_LIGHT_MODE_ENABLED": "1",
                "RAG_RETRIEVAL_BUDGET_MS": "1",
            },
            clear=False,
        ):
            with patch("apps.chat.services.rag.fetch_relevant_chunks", side_effect=lambda **kwargs: []):
                result = retrieve_for_chat(
                    user=self.user,
                    query_text="Qué dice el acuerdo de paris sobre mitigación",
                    allowed_documents=session.allowed_documents.all(),
                )
        self.assertIn(result.diagnostics.get("retrieval_mode"), {"light", "full"})
        self.assertIn(result.diagnostics.get("retrieval_skipped_reason"), {"budget_exceeded", None})

    @patch("apps.chat.api.views._build_chat_messages")
    @patch("apps.document.utils.client_openia.generate_chat_completion_stream")
    def test_stream_emits_early_status_event(self, mock_stream_completion, mock_build_chat):
        session = ChatSession.objects.create(owner=self.user, title="Stream")
        mock_build_chat.return_value = ([{"role": "user", "content": "hola"}], RetrievalResult())
        mock_stream_completion.return_value = iter(["ok"])

        url = reverse("chat-message-stream")
        with patch.dict(os.environ, {"RAG_STREAM_EARLY_EVENT_ENABLED": "1"}, clear=False):
            response = self.client.post(
                url,
                {"session": session.id, "content": "hola"},
                format="json",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn('"type": "status"', body)
        self.assertIn('"phase": "retrieval"', body)























