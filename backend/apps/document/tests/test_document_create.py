from io import BytesIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument, ProjectShareRole

User = get_user_model()


class DocumentCreateWithProjectSlugTest(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@test.com", password="TestPass123!", username="owner"
        )
        self.other = User.objects.create_user(
            email="other@test.com", password="TestPass123!", username="other"
        )
        self.project = Project.objects.create(
            owner=self.owner, name="Test Project"
        )
        self.client.force_authenticate(self.owner)

    def _make_file(self, name="test.txt", content=b"file content"):
        return SimpleUploadedFile(name, content, content_type="text/plain")

    def test_create_document_without_project(self):
        url = reverse("documentcreate")
        data = {"file": self._make_file()}
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Document.objects.filter(id=response.data["id"]).exists())

    def test_create_document_with_project_slug(self):
        url = reverse("documentcreate")
        data = {
            "file": self._make_file(),
            "project_slug": self.project.slug,
        }
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        doc = Document.objects.get(id=response.data["id"])
        self.assertTrue(
            self.project.documents.filter(id=doc.id).exists()
        )

    def test_create_document_rejects_is_public_during_upload(self):
        url = reverse("documentcreate")
        data = {
            "file": self._make_file(),
            "is_public": "true",
        }
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("is_public", response.data)

    def test_create_document_with_invalid_project_slug(self):
        url = reverse("documentcreate")
        data = {
            "file": self._make_file(),
            "project_slug": "nonexistent-slug",
        }
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_slug", response.data)

    def test_create_document_denied_for_non_editor(self):
        self.client.force_authenticate(self.other)
        url = reverse("documentcreate")
        data = {
            "file": self._make_file(),
            "project_slug": self.project.slug,
        }
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_slug", response.data)

    def test_create_document_allowed_for_editor_share(self):
        self.project.shares.create(user=self.other, role=ProjectShareRole.EDITOR)
        self.client.force_authenticate(self.other)
        url = reverse("documentcreate")
        data = {
            "file": self._make_file(),
            "project_slug": self.project.slug,
        }
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        doc = Document.objects.get(id=response.data["id"])
        self.assertTrue(self.project.documents.filter(id=doc.id).exists())

    def test_upload_with_project_proposes_evidence_tags(self):
        """
        Subir un documento nuevo directo a una operación tiene que proponerle
        etiquetas igual que vincular uno ya existente desde la biblioteca
        (``ProjectViewSet.add_documents``) — antes este camino se quedaba
        callado y el documento entraba sin ninguna, para siempre.
        """
        url = reverse("documentcreate")
        data = {"file": self._make_file(), "project_slug": self.project.slug}
        with patch(
            "apps.project.services.evidence_tags.apply_default_tags"
        ) as mocked:
            response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        link = ProjectDocument.objects.get(
            project=self.project, document_id=response.data["id"]
        )
        mocked.assert_called_once_with(link)

    def test_upload_without_project_does_not_touch_tags(self):
        url = reverse("documentcreate")
        data = {"file": self._make_file()}
        with patch(
            "apps.project.services.evidence_tags.apply_default_tags"
        ) as mocked:
            response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        mocked.assert_not_called()

    def test_unauthenticated_create_denied(self):
        self.client.force_authenticate(user=None)
        url = reverse("documentcreate")
        data = {"file": self._make_file()}
        response = self.client.post(url, data, format="multipart")

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
