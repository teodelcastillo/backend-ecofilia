from io import BytesIO

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

    def _tag(self, slug, source_topics=()):
        from apps.document.models import EvidenceTag

        tag, _ = EvidenceTag.objects.update_or_create(
            slug=slug, defaults={"name": slug.upper(), "source_topics": list(source_topics)}
        )
        return tag

    def test_upload_with_evidence_tags_tags_the_document(self):
        """`apiClient.upload` manda los arrays como string JSON."""
        self._tag("ndc")
        self._tag("nap")
        url = reverse("documentcreate")
        data = {
            "file": self._make_file(),
            "project_slug": self.project.slug,
            "evidence_tags": '["ndc", "nap"]',
        }
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        doc = Document.objects.get(id=response.data["id"])
        self.assertEqual(sorted(doc.evidence_tags.values_list("slug", flat=True)), ["nap", "ndc"])
        self.assertEqual(sorted(response.data["evidence_tags"]), ["nap", "ndc"])
        # El vínculo a la operación no guarda nada propio: hereda.
        link = ProjectDocument.objects.get(project=self.project, document=doc)
        self.assertFalse(link.tags_overridden)
        self.assertEqual(link.tags.count(), 0)

    def test_upload_with_unknown_tag_is_rejected(self):
        url = reverse("documentcreate")
        data = {"file": self._make_file(), "evidence_tags": "no-existe"}
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("evidence_tags", response.data)

    def test_upload_keeps_the_metadata_the_dialog_sends(self):
        """Temas, año, región y fuente se descartaban en silencio."""
        url = reverse("documentcreate")
        data = {
            "file": self._make_file(),
            "topics": '["NDCS: Contribuciones", "clima"]',
            "year": "2021",
            "region": "Perú",
            "source": "CMNUCC",
        }
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        doc = Document.objects.get(id=response.data["id"])
        self.assertEqual(doc.year, 2021)
        self.assertEqual(doc.region, "Perú")
        self.assertEqual(doc.source, "CMNUCC")
        self.assertIn("clima", doc.topics)

    def test_upload_with_topics_and_no_tags_gets_the_suggested_tag(self):
        self._tag("ndc", ["ndcs"])
        url = reverse("documentcreate")
        data = {"file": self._make_file(), "topics": "ndcs: contribuciones"}
        response = self.client.post(url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        doc = Document.objects.get(id=response.data["id"])
        self.assertEqual(list(doc.evidence_tags.values_list("slug", flat=True)), ["ndc"])

    def test_explicit_tags_win_over_the_topic_suggestion(self):
        self._tag("ndc", ["ndcs"])
        self._tag("nap")
        url = reverse("documentcreate")
        data = {"file": self._make_file(), "topics": "ndcs", "evidence_tags": "nap"}
        response = self.client.post(url, data, format="multipart")

        doc = Document.objects.get(id=response.data["id"])
        self.assertEqual(list(doc.evidence_tags.values_list("slug", flat=True)), ["nap"])

    def test_unauthenticated_create_denied(self):
        self.client.force_authenticate(user=None)
        url = reverse("documentcreate")
        data = {"file": self._make_file()}
        response = self.client.post(url, data, format="multipart")

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
