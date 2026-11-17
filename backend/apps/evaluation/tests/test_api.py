from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.chat.tests.retrieval_mock import RetrievalMock
from apps.document.models import Document
from apps.evaluation.models import Evaluation, EvaluationRunStatus
from apps.evaluation.tasks import run_evaluation_sync
from apps.project.models import Project, ProjectDocument

User = get_user_model()


class EvaluationAPITestCase(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com",
            password="secret123",
            username="owner",
        )
        self.viewer = User.objects.create_user(
            email="viewer@example.com",
            password="secret123",
            username="viewer",
        )
        self.other = User.objects.create_user(
            email="other@example.com",
            password="secret123",
            username="other",
        )
        self.doc_owned = Document.objects.create(
            owner=self.owner,
            name="Doc 1",
            slug="doc-1",
            is_public=False,
        )
        self.doc_public = Document.objects.create(
            owner=self.other,
            name="Doc Público",
            slug="doc-publico",
            is_public=True,
        )
        self.project = Project.objects.create(owner=self.owner, name="Proyecto 1")
        ProjectDocument.objects.create(
            project=self.project,
            document=self.doc_owned,
            added_by=self.owner,
        )
        self.client.force_authenticate(self.owner)

    def _create_evaluation_payload(self):
        return {
            "title": "Evaluación ESG",
            "description": "Analiza pilares clave.",
            "visibility": "private",
            "document_slugs": ["doc-1", "doc-publico"],
            "pillars": [
                {
                    "title": "Pilar 1",
                    "context_instructions": "Revisar impacto ambiental.",
                    "metrics": [
                        {
                            "title": "KPI 1",
                            "instructions": "Describe emisiones.",
                            "response_type": "qualitative",
                        },
                        {
                            "title": "KPI 2",
                            "instructions": "Califica emisiones.",
                            "response_type": "quantitative",
                            "scale_min": 0,
                            "scale_max": 5,
                        },
                    ],
                }
            ],
        }

    def test_create_evaluation_with_documents_and_pillars(self):
        url = reverse("evaluation-list")
        payload = self._create_evaluation_payload()
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data["documents"]), 2)
        self.assertEqual(len(response.data["pillars"]), 1)
        self.assertEqual(len(response.data["pillars"][0]["metrics"]), 2)

    def test_share_management(self):
        evaluation = self._create_evaluation()
        url = reverse("evaluation-shares", kwargs={"slug": evaluation.slug})
        # Se comparte por email (`resolve_share_target`), como el resto de los
        # recursos compartibles; `user_id` ya no es parte del contrato.
        payload = {"user_email": self.viewer.email, "role": "editor"}
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["role"], "editor")

        list_response = self.client.get(url)
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)

        share_id = response.data["id"]
        detail_url = reverse(
            "evaluation-share-detail",
            kwargs={"slug": evaluation.slug, "share_id": share_id},
        )
        patch_response = self.client.patch(
            detail_url, {"role": "viewer"}, format="json"
        )
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_response.data["role"], "viewer")

        delete_response = self.client.delete(detail_url)
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)

    @patch("apps.evaluation.api.views.run_evaluation_task")
    @patch("apps.evaluation.services.generate_chat_completion")
    @patch("apps.evaluation.services.retrieve_for_chat", new_callable=RetrievalMock)
    def test_run_creation_uses_project_documents(
        self, mock_fetch_chunks, mock_completion, mock_task
    ):
        evaluation = self._create_evaluation()
        chunk = SimpleNamespace(
            id=999,
            document=self.doc_owned,
            document_id=self.doc_owned.id,
            chunk_index=0,
            content="Contenido relevante",
            distance=0.1,
        )
        mock_fetch_chunks.return_value = [chunk]
        mock_completion.return_value = ("Resultado final 4", {"total_tokens": 15})
        mock_task.delay.side_effect = lambda run_id: run_evaluation_sync(run_id)

        url = reverse("evaluation-runs", kwargs={"slug": evaluation.slug})
        payload = {"project_slug": self.project.slug}
        response = self.client.post(url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], EvaluationRunStatus.COMPLETED)
        self.assertGreater(len(response.data["pillar_results"]), 0)
        mock_task.delay.assert_called_once()

    @patch("apps.evaluation.api.views.run_evaluation_task")
    @patch("apps.evaluation.services.generate_chat_completion")
    @patch("apps.evaluation.services.retrieve_for_chat", new_callable=RetrievalMock)
    def test_run_failure_is_recorded(
        self, mock_fetch_chunks, mock_completion, mock_task
    ):
        evaluation = self._create_evaluation()
        chunk = SimpleNamespace(
            id=111,
            document=self.doc_owned,
            document_id=self.doc_owned.id,
            chunk_index=0,
            content="Contenido relevante",
        )
        mock_fetch_chunks.return_value = [chunk]
        mock_completion.side_effect = RuntimeError("LLM error")
        mock_task.delay.side_effect = lambda run_id: run_evaluation_sync(run_id)

        url = reverse("evaluation-runs", kwargs={"slug": evaluation.slug})
        response = self.client.post(url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["status"], EvaluationRunStatus.FAILED)
        self.assertIn("LLM error", response.data["error_message"])

    def test_viewer_cannot_create_run(self):
        evaluation = self._create_evaluation()
        evaluation.shares.create(user=self.viewer, role="viewer")
        self.client.force_authenticate(self.viewer)
        url = reverse("evaluation-runs", kwargs={"slug": evaluation.slug})
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def _create_evaluation(self) -> Evaluation:
        payload = self._create_evaluation_payload()
        response = self.client.post(reverse("evaluation-list"), payload, format="json")
        evaluation = Evaluation.objects.get(pk=response.data["id"])
        evaluation.project = self.project
        evaluation.save()
        return evaluation

