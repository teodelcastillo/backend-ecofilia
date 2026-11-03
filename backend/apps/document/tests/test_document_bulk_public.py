from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document

User = get_user_model()


class DocumentBulkPublicAPITest(APITestCase):
    def setUp(self):
        # Publicar es de superadmin y exige MFA activo (ADMIN_REQUIRE_MFA).
        self.superuser = User.objects.create_superuser(
            email="admin@test.com",
            password="TestPass123!",
            username="admin@test.com",
            mfa_enabled=True,
        )
        self.regular = User.objects.create_user(
            email="user@test.com",
            password="TestPass123!",
            username="user@test.com",
        )
        self.admin_user = User.objects.create_user(
            email="admin-role@test.com",
            password="TestPass123!",
            username="admin-role@test.com",
            role="admin",
            mfa_enabled=True,
        )
        self.doc_a = Document.objects.create(
            owner=self.superuser,
            name="A",
            slug="doc-a-bulk-pub",
            is_public=False,
        )
        self.doc_b = Document.objects.create(
            owner=self.superuser,
            name="B",
            slug="doc-b-bulk-pub",
            is_public=False,
        )
        self.other_doc = Document.objects.create(
            owner=self.regular,
            name="Other",
            slug="doc-other-bulk-pub",
            is_public=False,
        )

    def test_superuser_sets_public_on_own_documents(self):
        self.client.force_authenticate(self.superuser)
        url = reverse("documentbulkpublic")
        response = self.client.post(
            url,
            {"slugs": [self.doc_a.slug, self.doc_b.slug], "is_public": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated"], 2)
        self.assertEqual(response.data["matched"], 2)
        self.assertEqual(response.data["requested"], 2)
        self.doc_a.refresh_from_db()
        self.doc_b.refresh_from_db()
        self.assertTrue(self.doc_a.is_public)
        self.assertTrue(self.doc_b.is_public)

    def test_superuser_can_change_any_document_for_public_library(self):
        self.client.force_authenticate(self.superuser)
        url = reverse("documentbulkpublic")
        response = self.client.post(
            url,
            {"slugs": [self.other_doc.slug], "is_public": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated"], 1)
        self.assertEqual(response.data["matched"], 1)
        self.other_doc.refresh_from_db()
        self.assertTrue(self.other_doc.is_public)

    def test_admin_role_user_can_change_document_visibility(self):
        self.client.force_authenticate(self.admin_user)
        url = reverse("documentbulkpublic")
        response = self.client.post(
            url,
            {"slugs": [self.other_doc.slug], "is_public": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated"], 1)
        self.other_doc.refresh_from_db()
        self.assertTrue(self.other_doc.is_public)

    def test_regular_user_forbidden(self):
        self.client.force_authenticate(self.regular)
        url = reverse("documentbulkpublic")
        response = self.client.post(
            url,
            {"slugs": [self.other_doc.slug], "is_public": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_superuser_unpublish(self):
        self.doc_a.is_public = True
        self.doc_a.save(update_fields=["is_public"])
        self.client.force_authenticate(self.superuser)
        url = reverse("documentbulkpublic")
        response = self.client.post(
            url,
            {"slugs": [self.doc_a.slug], "is_public": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated"], 1)
        self.doc_a.refresh_from_db()
        self.assertFalse(self.doc_a.is_public)
