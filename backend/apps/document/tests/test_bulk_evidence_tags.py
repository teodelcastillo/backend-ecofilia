"""Etiquetado de evidencia en lote: sólo superadmins, agrega y quita sin pisar."""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document, EvidenceTag

User = get_user_model()


class BulkEvidenceTagsTestCase(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="su@example.com", password="x", username="su"
        )
        self.user = User.objects.create_user(email="u@example.com", password="x", username="u")
        self.ndc, _ = EvidenceTag.objects.update_or_create(slug="ndc", defaults={"name": "NDC"})
        self.nap, _ = EvidenceTag.objects.update_or_create(slug="nap", defaults={"name": "NAP"})
        self.a = Document.objects.create(owner=self.user, name="A", slug="a", is_public=True)
        self.b = Document.objects.create(owner=self.user, name="B", slug="b", is_public=True)
        self.b.evidence_tags.set([self.nap])
        self.url = reverse("documentbulkevidencetags")

    def _tags(self, doc):
        return sorted(doc.evidence_tags.values_list("slug", flat=True))

    def test_non_superadmin_is_rejected(self):
        self.client.force_authenticate(self.user)
        response = self.client.post(self.url, {"slugs": ["a"], "add": ["ndc"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self._tags(self.a), [])

    def test_add_keeps_existing_tags(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.url, {"slugs": ["a", "b"], "add": ["ndc"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._tags(self.a), ["ndc"])
        self.assertEqual(self._tags(self.b), ["nap", "ndc"])

    def test_add_twice_is_idempotent(self):
        self.client.force_authenticate(self.admin)
        for _ in range(2):
            self.client.post(self.url, {"slugs": ["b"], "add": ["nap"]}, format="json")
        self.assertEqual(self._tags(self.b), ["nap"])

    def test_remove(self):
        self.client.force_authenticate(self.admin)
        self.client.post(self.url, {"slugs": ["a", "b"], "remove": ["nap"]}, format="json")
        self.assertEqual(self._tags(self.b), [])

    def test_needs_add_or_remove(self):
        self.client.force_authenticate(self.admin)
        response = self.client.post(self.url, {"slugs": ["a"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_list_filters_by_tag_and_untagged(self):
        self.client.force_authenticate(self.admin)
        base = "/api/document/list/?scope=public&paginate=1"
        untagged = self.client.get(base + "&evidence_tag=__none__").data["results"]
        tagged = self.client.get(base + "&evidence_tag=nap").data["results"]
        self.assertEqual([d["slug"] for d in untagged], ["a"])
        self.assertEqual([d["slug"] for d in tagged], ["b"])
