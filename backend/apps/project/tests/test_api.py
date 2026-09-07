from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument, ProjectShareRole

User = get_user_model()


class ProjectAPITestCase(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="secret123", username="owner"
        )
        self.other = User.objects.create_user(
            email="other@example.com", password="secret123", username="other"
        )
        self.viewer = User.objects.create_user(
            email="viewer@example.com", password="secret123", username="viewer"
        )
        self.doc_owned = Document.objects.create(
            owner=self.owner, name="Doc Propio", slug="doc-propio"
        )
        self.doc_public = Document.objects.create(
            owner=self.other,
            name="Doc Público",
            slug="doc-publico",
            is_public=True,
        )
        self.doc_forbidden = Document.objects.create(
            owner=self.other,
            name="Doc Privado",
            slug="doc-privado",
            is_public=False,
        )
        self.client.force_authenticate(self.owner)

    def test_create_project_with_documents(self):
        url = reverse("project-list")
        payload = {
            "name": "Proyecto A",
            "description": "Descripción",
            "document_slugs": ["doc-propio", "doc-publico"],
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data["documents"]), 2)

    def test_create_project_with_blueprint_document(self):
        url = reverse("project-list")
        payload = {
            "name": "Proyecto con Blueprint",
            "document_slugs": ["doc-propio", "doc-publico"],
            "blueprint_document_slug": "doc-publico",
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["blueprint_document_slug"], "doc-publico")

    def test_reject_blueprint_not_in_linked_documents(self):
        url = reverse("project-list")
        payload = {
            "name": "Proyecto inválido",
            "document_slugs": ["doc-propio"],
            "blueprint_document_slug": "doc-publico",
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("blueprint_document_slug", response.data)

    def test_create_project_rejects_forbidden_document(self):
        url = reverse("project-list")
        payload = {
            "name": "Proyecto B",
            "document_slugs": ["doc-privado"],
        }
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_document_action(self):
        project = Project.objects.create(owner=self.owner, name="Proyecto Base")
        ProjectDocument.objects.create(
            project=project, document=self.doc_owned, added_by=self.owner
        )
        url = reverse("project-add-document", kwargs={"slug": project.slug})
        payload = {"document_slugs": ["doc-publico"]}
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        slugs = {doc["slug"] for doc in response.data["documents"]}
        self.assertSetEqual(slugs, {"doc-propio", "doc-publico"})

    def test_remove_document_action(self):
        project = Project.objects.create(owner=self.owner, name="Proyecto Desvincular")
        ProjectDocument.objects.create(
            project=project, document=self.doc_owned, added_by=self.owner
        )
        ProjectDocument.objects.create(
            project=project, document=self.doc_public, added_by=self.owner
        )
        url = reverse(
            "project-remove-document",
            kwargs={"slug": project.slug, "document_slug": "doc-publico"},
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(
            list(project.project_documents.values_list("document__slug", flat=True)),
            ["doc-propio"],
        )

    def test_remove_primary_document_clears_blueprint(self):
        """Desvincular el principal deja la operación sin principal.

        Si el blueprint sobreviviera al desvinculado, la operación apuntaría a
        un documento fuera de su alcance: las corridas no lo verían —filtran
        por ``ProjectDocument``— pero la carátula lo seguiría mostrando.
        """
        project = Project.objects.create(owner=self.owner, name="Proyecto Principal")
        ProjectDocument.objects.create(
            project=project,
            document=self.doc_owned,
            added_by=self.owner,
            is_primary=True,
        )
        project.blueprint_document = self.doc_owned
        project.save(update_fields=["blueprint_document"])

        url = reverse(
            "project-remove-document",
            kwargs={"slug": project.slug, "document_slug": "doc-propio"},
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        project.refresh_from_db()
        self.assertIsNone(project.blueprint_document_id)

    def test_viewer_cannot_remove_documents(self):
        project = Project.objects.create(owner=self.owner, name="Proyecto Solo Lectura")
        ProjectDocument.objects.create(
            project=project, document=self.doc_owned, added_by=self.owner
        )
        project.shares.create(user=self.viewer, role=ProjectShareRole.VIEWER)
        self.client.force_authenticate(self.viewer)
        url = reverse(
            "project-remove-document",
            kwargs={"slug": project.slug, "document_slug": "doc-propio"},
        )
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(project.project_documents.count(), 1)

    def test_viewer_cannot_modify_documents(self):
        project = Project.objects.create(owner=self.owner, name="Proyecto Compartido")
        project.shares.create(user=self.viewer, role=ProjectShareRole.VIEWER)
        self.client.force_authenticate(self.viewer)
        url = reverse("project-add-document", kwargs={"slug": project.slug})
        response = self.client.post(
            url, {"document_slugs": ["doc-publico"]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_share_management(self):
        project = Project.objects.create(owner=self.owner, name="Proyecto Share")
        url = reverse("project-shares", kwargs={"slug": project.slug})
        payload = {"user_email": self.viewer.email, "role": ProjectShareRole.EDITOR}
        response = self.client.post(url, payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["role"], ProjectShareRole.EDITOR)

        list_response = self.client.get(url)
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.data), 1)

        share_id = response.data["id"]
        detail_url = reverse(
            "project-share-detail",
            kwargs={"slug": project.slug, "share_id": share_id},
        )
        patch_response = self.client.patch(
            detail_url,
            {"role": ProjectShareRole.VIEWER},
            format="json",
        )
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)
        self.assertEqual(patch_response.data["role"], ProjectShareRole.VIEWER)

        delete_response = self.client.delete(detail_url)
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)

    def test_shared_viewer_sees_all_skill_executions(self):
        from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillType

        project = Project.objects.create(owner=self.owner, name="Proyecto Outputs")
        project.shares.create(user=self.viewer, role=ProjectShareRole.VIEWER)
        skill = Skill.objects.create(
            name="Test Skill",
            slug="test-skill-exec",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            owner=self.owner,
        )
        SkillExecution.objects.create(
            skill=skill,
            owner=self.owner,
            project=project,
            status=ExecutionStatus.COMPLETED,
        )

        self.client.force_authenticate(self.viewer)
        url = reverse("project-skill-executions", kwargs={"slug": project.slug})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)



class ProjectDocumentTagsAPITestCase(APITestCase):
    """Las etiquetas de un documento dentro de una operación."""

    def setUp(self):
        self.owner = User.objects.create_user(
            email="op@example.com", password="secret123", username="op"
        )
        self.viewer = User.objects.create_user(
            email="lector@example.com", password="secret123", username="lector"
        )
        self.doc = Document.objects.create(
            owner=self.owner, name="NDC", slug="ndc-doc", topics=["ndcs"]
        )
        self.project = Project.objects.create(owner=self.owner, name="Operación Tags")
        ProjectDocument.objects.create(project=self.project, document=self.doc)
        self.client.force_authenticate(self.owner)
        self.url = reverse(
            "project-document-tags",
            kwargs={"slug": self.project.slug, "document_slug": "ndc-doc"},
        )

    def test_set_tags(self):
        response = self.client.put(self.url, {"tags": ["ndc", "nap"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(sorted(response.data["tags"]), ["nap", "ndc"])

    def test_tags_are_replaced_not_merged(self):
        """Sin reemplazo no habría forma de sacar una etiqueta mal puesta."""
        self.client.put(self.url, {"tags": ["ndc", "nap"]}, format="json")
        response = self.client.put(self.url, {"tags": ["ndc"]}, format="json")
        self.assertEqual(response.data["tags"], ["ndc"])

    def test_unknown_tag_is_rejected(self):
        response = self.client.put(self.url, {"tags": ["inventada"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_viewer_cannot_edit_tags(self):
        self.project.shares.create(user=self.viewer, role=ProjectShareRole.VIEWER)
        self.client.force_authenticate(self.viewer)
        response = self.client.put(self.url, {"tags": ["ndc"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_linking_a_document_proposes_tags_from_its_topics(self):
        """La biblioteca propone; la operación decide."""
        otro = Document.objects.create(
            owner=self.owner,
            name="NAP Colombia",
            slug="nap-colombia",
            topics=["NAPS: Planes Nacionales de Adaptación"],
        )
        url = reverse("project-add-document", kwargs={"slug": self.project.slug})
        response = self.client.post(
            url, {"document_slugs": [otro.slug]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        linked = next(
            d for d in response.data["documents"] if d["slug"] == "nap-colombia"
        )
        self.assertEqual(linked["tags"], ["nap"])
