"""
El dueño de una operación ve los documentos que sus colaboradores suben a ella.

Hasta octubre de 2026 la regla de visibilidad sólo contaba las operaciones
compartidas con el usuario, no las propias: el dueño recibía 404 al abrir un
documento que un editor había subido a su operación, 400 al abrir un chat con
los documentos de la operación, y el RAG descartaba esos fragmentos en
silencio. Un lector de la misma operación, en cambio, sí los veía.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.chat.services.rag import _scope_qs_for_user
from apps.document.models import Document, SmartChunk
from apps.document.services import accessible_documents_for
from apps.project.models import Project, ProjectDocument, ProjectShareRole

User = get_user_model()


class OperationDocumentVisibilityTestCase(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="x")
        self.editor = User.objects.create_user(email="editor@example.com", password="x")
        self.viewer = User.objects.create_user(email="viewer@example.com", password="x")
        self.stranger = User.objects.create_user(email="stranger@example.com", password="x")

        self.project = Project.objects.create(owner=self.owner, name="Operación")
        self.project.shares.create(user=self.editor, role=ProjectShareRole.EDITOR)
        self.project.shares.create(user=self.viewer, role=ProjectShareRole.VIEWER)

        self.doc = Document.objects.create(owner=self.editor, name="Anexo del editor", slug="anexo-editor")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.editor)
        self.chunk = SmartChunk.objects.create(
            document=self.doc, chunk_index=0, content="texto", token_count=1,
            embedding=[0.0] * 1536,
        )

    def test_owner_opens_document_added_by_collaborator(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(f"/api/document/{self.doc.slug}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(self.doc.can_view(self.owner))

    def test_owner_chats_with_all_operation_documents(self):
        self.client.force_authenticate(self.owner)
        url = reverse("project-chat-sessions", kwargs={"slug": self.project.slug})
        response = self.client.post(
            url, {"title": "Chat", "document_slugs": [self.doc.slug]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_owner_library_and_rag_scope_include_the_document(self):
        self.assertIn(self.doc, accessible_documents_for(self.owner, [self.doc.slug]))
        self.client.force_authenticate(self.owner)
        response = self.client.get("/api/document/list/", {"scope": "all"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertIn(self.doc.slug, [row["slug"] for row in rows])
        self.assertIn(self.chunk, _scope_qs_for_user(self.owner, [self.doc.id]))

    def test_viewer_still_sees_it(self):
        self.client.force_authenticate(self.viewer)
        response = self.client.get(f"/api/document/{self.doc.slug}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_outsider_still_does_not(self):
        self.client.force_authenticate(self.stranger)
        response = self.client.get(f"/api/document/{self.doc.slug}/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(self.doc.can_view(self.stranger))
        self.assertNotIn(self.chunk, _scope_qs_for_user(self.stranger, [self.doc.id]))

    def test_owning_another_operation_does_not_open_this_one(self):
        Project.objects.create(owner=self.stranger, name="Otra operación")
        self.assertFalse(accessible_documents_for(self.stranger, [self.doc.slug]).exists())
