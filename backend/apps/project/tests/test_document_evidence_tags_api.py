"""
Etiquetas de evidencia vistas desde la API: se asignan en el documento y la
operación las hereda, salvo que decida otras.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument

User = get_user_model()


class DocumentEvidenceTagsApiTestCase(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="secret123", username="owner"
        )
        self.client.force_authenticate(self.owner)
        self.ndc, _ = EvidenceTag.objects.update_or_create(
            slug="ndc", defaults={"name": "NDC", "source_topics": ["ndcs"]}
        )
        self.nap, _ = EvidenceTag.objects.update_or_create(
            slug="nap", defaults={"name": "NAP", "source_topics": ["naps"]}
        )
        self.doc = Document.objects.create(owner=self.owner, name="NDC", slug="ndc-doc")
        self.project = Project.objects.create(owner=self.owner, name="Operación")
        self.link = ProjectDocument.objects.create(project=self.project, document=self.doc)

    def _doc_entry(self):
        response = self.client.get(reverse("project-detail", kwargs={"slug": self.project.slug}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return next(d for d in response.data["documents"] if d["slug"] == self.doc.slug)

    def _patch_document(self, payload):
        return self.client.patch(
            reverse("document-detail", kwargs={"slug": self.doc.slug}), payload, format="json"
        )

    def test_tagging_the_document_reaches_the_operation(self):
        response = self._patch_document({"evidence_tags": ["ndc"]})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        entry = self._doc_entry()
        self.assertEqual(entry["tags"], ["ndc"])
        self.assertEqual(entry["document_tags"], ["ndc"])
        self.assertFalse(entry["tags_overridden"])

    def test_editing_topics_proposes_a_tag_only_if_there_is_none(self):
        self._patch_document({"topics": ["NDCS: Contribuciones"]})
        self.assertEqual(list(self.doc.evidence_tags.values_list("slug", flat=True)), ["ndc"])

        self.doc.evidence_tags.set([self.nap])
        self._patch_document({"topics": ["ndcs"]})
        self.assertEqual(list(self.doc.evidence_tags.values_list("slug", flat=True)), ["nap"])

    def test_emptying_tags_on_purpose_is_respected(self):
        self.doc.evidence_tags.set([self.nap])
        self._patch_document({"topics": ["ndcs"], "evidence_tags": []})
        self.assertEqual(self.doc.evidence_tags.count(), 0)

    def test_operation_override_and_back(self):
        self.doc.evidence_tags.set([self.ndc])
        url = reverse(
            "project-document-tags",
            kwargs={"slug": self.project.slug, "document_slug": self.doc.slug},
        )

        response = self.client.put(url, {"tags": ["nap"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["tags"], ["nap"])
        self.assertTrue(response.data["tags_overridden"])
        self.assertEqual(response.data["document_tags"], ["ndc"])

        # Un cambio en la biblioteca no pisa lo que decidió la operación.
        self.doc.evidence_tags.set([self.ndc, self.nap])
        self.assertEqual(self._doc_entry()["tags"], ["nap"])

        response = self.client.put(url, {"inherit": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["tags_overridden"])
        self.assertEqual(sorted(response.data["tags"]), ["nap", "ndc"])

    def test_put_without_tags_nor_inherit_is_rejected(self):
        url = reverse(
            "project-document-tags",
            kwargs={"slug": self.project.slug, "document_slug": self.doc.slug},
        )
        response = self.client.put(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_patch_with_document_slugs_keeps_existing_links(self):
        """Antes borraba y recreaba todos los vínculos, y con ellos lo decidido."""
        url = reverse(
            "project-document-tags",
            kwargs={"slug": self.project.slug, "document_slug": self.doc.slug},
        )
        self.client.put(url, {"tags": ["nap"]}, format="json")
        otro = Document.objects.create(owner=self.owner, name="Otro", slug="otro")

        response = self.client.patch(
            reverse("project-detail", kwargs={"slug": self.project.slug}),
            {"document_slugs": [self.doc.slug, otro.slug]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        link = ProjectDocument.objects.get(project=self.project, document=self.doc)
        self.assertEqual(link.pk, self.link.pk)
        self.assertTrue(link.tags_overridden)
        self.assertTrue(
            ProjectDocument.objects.filter(project=self.project, document=otro).exists()
        )

    def test_evidence_tag_catalog_counts_documents(self):
        self.doc.evidence_tags.set([self.ndc])
        response = self.client.get("/api/document/evidence-tags/")
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        ndc = next(r for r in rows if r["slug"] == "ndc")
        self.assertEqual(ndc["document_count"], 1)

    def test_the_catalog_says_the_group_of_each_tag(self):
        EvidenceTag.objects.update_or_create(slug="propia", defaults={"name": "Propia"})
        response = self.client.get("/api/document/evidence-tags/")
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        groups = {r["slug"]: r["group"] for r in rows}
        self.assertEqual(groups["ido"], "principal")
        self.assertEqual(groups["dec"], "principal")
        for slug in ("ias", "analisis-riesgos", "matriz-resultados", "disenos-proyecto", "documento-operacion"):
            self.assertEqual(groups[slug], "operacion", slug)
        for slug in ("ndc", "nap", "lts", "comunicacion-adaptacion", "nbsap", "pancd", "inventario-gei"):
            self.assertEqual(groups[slug], "contexto", slug)
        self.assertEqual(groups["metodologia-caf"], "otro")
        self.assertEqual(groups["propia"], "otro")


class PrincipalStageTagTestCase(APITestCase):
    """El principal lleva la etiqueta de la etapa en que se cargó (IDO / DEC),
    como decisión de la operación: el mismo archivo puede ser anexo en otra."""

    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="x", username="owner")
        self.client.force_authenticate(self.owner)
        self.doc = Document.objects.create(owner=self.owner, name="IDO", slug="ido-doc")
        self.doc.evidence_tags.set([EvidenceTag.objects.get(slug="documento-operacion")])
        self.project = Project.objects.create(
            owner=self.owner, name="Operación", context_notes={"estado": "ido_cci"}
        )
        self.link = ProjectDocument.objects.create(project=self.project, document=self.doc)
        self.detail = reverse("project-detail", kwargs={"slug": self.project.slug})
        self.tags_url = reverse(
            "project-document-tags", kwargs={"slug": self.project.slug, "document_slug": self.doc.slug}
        )

    def _entry(self):
        response = self.client.get(self.detail)
        return next(d for d in response.data["documents"] if d["slug"] == self.doc.slug)

    def test_marking_the_principal_adds_the_stage_tag_as_an_override(self):
        response = self.client.patch(self.detail, {"blueprint_document_slug": self.doc.slug}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        entry = self._entry()
        self.assertTrue(entry["is_primary"])
        self.assertTrue(entry["tags_overridden"])
        self.assertEqual(sorted(entry["tags"]), ["documento-operacion", "ido"])
        # La biblioteca no cambia.
        self.assertEqual(entry["document_tags"], ["documento-operacion"])

    def test_in_dec_the_tag_is_dec_and_an_existing_one_is_kept(self):
        self.project.context_notes = {"estado": "dec"}
        self.project.save(update_fields=["context_notes"])
        self.client.patch(self.detail, {"blueprint_document_slug": self.doc.slug}, format="json")
        self.assertIn("dec", self._entry()["tags"])

        # Marcado IDO en IDO y todavía principal en DEC: no se le suma «dec».
        other = Document.objects.create(owner=self.owner, name="Otro", slug="otro")
        other.evidence_tags.set([EvidenceTag.objects.get(slug="ido")])
        ProjectDocument.objects.create(project=self.project, document=other)
        self.client.patch(self.detail, {"blueprint_document_slug": other.slug}, format="json")
        entry = next(
            d for d in self.client.get(self.detail).data["documents"] if d["slug"] == other.slug
        )
        self.assertEqual(entry["tags"], ["ido"])
        self.assertFalse(entry["tags_overridden"])

    def test_without_a_stage_there_is_no_stage_tag(self):
        self.project.context_notes = {"estado": "pre_originacion"}
        self.project.save(update_fields=["context_notes"])
        self.client.patch(self.detail, {"blueprint_document_slug": self.doc.slug}, format="json")
        entry = self._entry()
        self.assertEqual(entry["tags"], ["documento-operacion"])
        self.assertFalse(entry["tags_overridden"])

    def test_retagging_the_principal_keeps_its_stage_tag(self):
        self.client.patch(self.detail, {"blueprint_document_slug": self.doc.slug}, format="json")
        response = self.client.put(self.tags_url, {"tags": ["ias"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(sorted(response.data["tags"]), ["ias", "ido"])

        # Volver a las del documento tampoco lo deja sin etiqueta de etapa.
        response = self.client.put(self.tags_url, {"inherit": True}, format="json")
        self.assertEqual(sorted(response.data["tags"]), ["documento-operacion", "ido"])

    def test_a_document_that_is_not_the_principal_is_left_alone(self):
        response = self.client.put(self.tags_url, {"tags": ["ias"]}, format="json")
        self.assertEqual(response.data["tags"], ["ias"])
