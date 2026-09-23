from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument

User = get_user_model()


class DocumentBulkCreateTest(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="bulk@test.com", password="TestPass123!", username="bulkowner"
        )
        self.project = Project.objects.create(
            owner=self.owner, name="Bulk Project"
        )
        self.client.force_authenticate(self.owner)
        self.url = reverse("documentbulkcreate")

    def _make_file(self, name="test.txt", content=b"content", size=None):
        f = SimpleUploadedFile(name, content, content_type="text/plain")
        if size is not None:
            f.size = size
        return f

    def test_bulk_create_all_success(self):
        files = [self._make_file(f"file{i}.txt") for i in range(3)]
        data = {"files": files}
        response = self.client.post(self.url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data["successful"]), 3)
        self.assertEqual(len(response.data["failed"]), 0)
        self.assertEqual(response.data["created"], 3)

    def test_bulk_create_with_project_slug(self):
        files = [self._make_file(f"proj{i}.txt") for i in range(2)]
        data = {"files": files, "project_slug": self.project.slug}
        response = self.client.post(self.url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data["successful"]), 2)

        for entry in response.data["successful"]:
            doc = Document.objects.get(id=entry["id"])
            self.assertTrue(self.project.documents.filter(id=doc.id).exists())

    def test_bulk_create_tags_every_document(self):
        """El multipart repite el campo, un valor por etiqueta."""
        from apps.document.models import EvidenceTag

        for slug in ("ndc", "nap"):
            EvidenceTag.objects.update_or_create(slug=slug, defaults={"name": slug})
        files = [self._make_file(f"tag{i}.txt") for i in range(2)]
        data = {
            "files": files,
            "project_slug": self.project.slug,
            "evidence_tags": ["ndc", "nap"],
            "topics": ["clima", "agua"],
            "region": "Chile",
        }
        response = self.client.post(self.url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        for entry in response.data["successful"]:
            doc = Document.objects.get(id=entry["id"])
            self.assertEqual(
                sorted(doc.evidence_tags.values_list("slug", flat=True)), ["nap", "ndc"]
            )
            self.assertEqual(doc.topics, ["clima", "agua"])
            self.assertEqual(doc.region, "Chile")

    def test_bulk_create_invalid_project_slug(self):
        files = [self._make_file("f.txt")]
        data = {"files": files, "project_slug": "nope-not-real"}
        response = self.client.post(self.url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_slug", response.data)

    def test_bulk_create_no_files_returns_400(self):
        response = self.client.post(self.url, {}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_bulk_create_with_metadata(self):
        files = [self._make_file("meta.txt")]
        data = {
            "files": files,
            "category": "reports",
            "description": "Quarterly report",
        }
        response = self.client.post(self.url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        doc_id = response.data["successful"][0]["id"]
        doc = Document.objects.get(id=doc_id)
        self.assertEqual(doc.category, "reports")
        self.assertEqual(doc.description, "Quarterly report")

    def test_bulk_create_multiple_files_ignores_shared_name_and_keeps_category(self):
        files = [self._make_file(f"multi{i}.txt") for i in range(3)]
        data = {
            "files": files,
            "name": "Shared Name",
            "category": "reports",
        }
        response = self.client.post(self.url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data["successful"]), 3)
        self.assertEqual(len(response.data["failed"]), 0)

        created_ids = [entry["id"] for entry in response.data["successful"]]
        docs = Document.objects.filter(id__in=created_ids)
        self.assertEqual(docs.count(), 3)
        self.assertTrue(all(doc.category == "reports" for doc in docs))

    def test_bulk_create_response_has_backward_compat_keys(self):
        files = [self._make_file("compat.txt")]
        data = {"files": files}
        response = self.client.post(self.url, data, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("successful", response.data)
        self.assertIn("created", response.data)
        self.assertIn("documents", response.data)

    def test_bulk_unauthenticated_denied(self):
        self.client.force_authenticate(user=None)
        files = [self._make_file("unauth.txt")]
        data = {"files": files}
        response = self.client.post(self.url, data, format="multipart")

        self.assertIn(
            response.status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )
