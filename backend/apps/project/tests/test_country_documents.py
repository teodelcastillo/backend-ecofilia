"""
Asignación automática de instrumentos país (NDC, NAP, LTS, AC).

Lo que importa acá es la regla de negocio: entre varias versiones del mismo
instrumento para un país, sólo la más nueva queda vinculada y etiquetada — y
eso se mantiene así aunque aparezca una versión más nueva después.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument
from apps.project.services.evidence_tags import effective_tag_slugs, override_tags
from apps.project.services.country_documents import (
    country_instrument_documents,
    sync_country_instrument_documents,
)

User = get_user_model()


def _backdate(document: Document, when):
    # `created_at` es `auto_now_add`: sólo se puede pisar con un UPDATE crudo.
    Document.objects.filter(pk=document.pk).update(created_at=when)
    document.refresh_from_db()


class CountryInstrumentDocumentsTestCase(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="owner@example.com", password="secret123", username="owner"
        )
        self.other = User.objects.create_user(
            email="other@example.com", password="secret123", username="other"
        )
        # Las etiquetas de semilla ya existen (migración 0016): se toman las
        # que hay en vez de crear duplicados.
        self.tag_ndc, _ = EvidenceTag.objects.update_or_create(
            slug="ndc", defaults={"name": "NDC", "source_topics": ["ndcs"]}
        )
        self.client.force_authenticate(self.owner)

    def _make_ndc(self, slug, *, owner=None, region="Colombia", is_public=True, age_days=0, year=None):
        doc = Document.objects.create(
            owner=owner or self.owner,
            name=f"NDC {slug}",
            slug=slug,
            region=region,
            is_public=is_public,
            year=year,
        )
        doc.evidence_tags.set([self.tag_ndc])
        _backdate(doc, timezone.now() - timedelta(days=age_days))
        return doc

    def test_only_latest_ndc_is_selected(self):
        old = self._make_ndc("ndc-2016", age_days=10)
        new = self._make_ndc("ndc-2021", age_days=1)

        by_tag = country_instrument_documents(self.owner, "Colombia", ["ndc"])

        self.assertEqual(by_tag["ndc"], [new, old])

    def test_sync_links_and_tags_the_latest_only(self):
        old = self._make_ndc("ndc-2016", age_days=10)
        new = self._make_ndc("ndc-2021", age_days=1)
        project = Project.objects.create(
            owner=self.owner, name="Operación", context_notes={"pais": "Colombia"}
        )

        assigned = sync_country_instrument_documents(project)

        self.assertEqual(assigned["ndc"], new)
        new_link = ProjectDocument.objects.get(project=project, document=new)
        self.assertEqual(effective_tag_slugs(new_link), ["ndc"])
        self.assertFalse(
            ProjectDocument.objects.filter(project=project, document=old).exists()
        )

    def test_sync_untags_a_superseded_version_without_unlinking_it(self):
        old = self._make_ndc("ndc-2016", age_days=10)
        project = Project.objects.create(
            owner=self.owner, name="Operación", context_notes={"pais": "Colombia"}
        )
        sync_country_instrument_documents(project)
        old_link = ProjectDocument.objects.get(project=project, document=old)
        self.assertEqual(effective_tag_slugs(old_link), ["ndc"])

        new = self._make_ndc("ndc-2021", age_days=1)
        sync_country_instrument_documents(project)

        old_link.refresh_from_db()
        new_link = ProjectDocument.objects.get(project=project, document=new)
        self.assertEqual(effective_tag_slugs(old_link), [])
        self.assertTrue(old_link.tags_overridden)
        self.assertEqual(effective_tag_slugs(new_link), ["ndc"])
        # Sigue vinculada: sólo se le sacó la etiqueta, no se desvinculó.
        self.assertTrue(
            ProjectDocument.objects.filter(project=project, document=old).exists()
        )

    def test_private_document_from_another_owner_is_not_assigned(self):
        self._make_ndc("ndc-ajeno", owner=self.other, is_public=False, age_days=1)
        project = Project.objects.create(
            owner=self.owner, name="Operación", context_notes={"pais": "Colombia"}
        )

        assigned = sync_country_instrument_documents(project)

        self.assertNotIn("ndc", assigned)

    def test_no_country_is_a_noop(self):
        self._make_ndc("ndc-2021", age_days=1)
        project = Project.objects.create(owner=self.owner, name="Operación")

        assigned = sync_country_instrument_documents(project)

        self.assertEqual(assigned, {})

    def test_create_project_via_api_auto_links_latest_ndc(self):
        self._make_ndc("ndc-2016", age_days=10)
        new = self._make_ndc("ndc-2021", age_days=1)

        response = self.client.post(
            reverse("project-list"),
            {"name": "Operación Colombia", "context_notes": {"pais": "Colombia"}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        slugs = {d["slug"] for d in response.data["documents"]}
        self.assertIn(new.slug, slugs)
        ndc_entry = next(d for d in response.data["documents"] if d["slug"] == new.slug)
        self.assertIn("ndc", ndc_entry["tags"])

    def test_update_project_country_resyncs(self):
        self._make_ndc("ndc-colombia", region="Colombia", age_days=1)
        peru_doc = self._make_ndc("ndc-peru", region="Perú", age_days=1)
        project = Project.objects.create(owner=self.owner, name="Operación")

        response = self.client.patch(
            reverse("project-detail", kwargs={"slug": project.slug}),
            {"context_notes": {"pais": "Perú"}},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # DB, no `response.data`: la instancia que arma la respuesta se leyó
        # con los documentos prefetcheados *antes* de este mismo save, así
        # que su `documents` no refleja lo que el propio request acaba de
        # vincular (el frontend nunca confía en este body: siempre invalida
        # la query y refetchea aparte).
        link = ProjectDocument.objects.get(project=project, document=peru_doc)
        self.assertEqual(effective_tag_slugs(link), ["ndc"])

    def test_a_tag_removed_in_the_operation_is_not_put_back(self):
        """Guardar la operación no deshace lo que decidió el ejecutivo."""
        ndc = self._make_ndc("ndc-2021", age_days=1)
        project = Project.objects.create(
            owner=self.owner, name="Operación", context_notes={"pais": "Colombia"}
        )
        sync_country_instrument_documents(project)
        link = ProjectDocument.objects.get(project=project, document=ndc)
        override_tags(link, [])

        sync_country_instrument_documents(project)

        link.refresh_from_db()
        self.assertEqual(effective_tag_slugs(link), [])

    def test_the_newest_year_wins_over_the_latest_upload(self):
        """Una NDC de 2016 subida tarde no desplaza a la de 2021."""
        vieja = self._make_ndc("ndc-2016", year=2016, age_days=1)
        nueva = self._make_ndc("ndc-2021", year=2021, age_days=30)

        by_tag = country_instrument_documents(self.owner, "Colombia", ["ndc"])

        self.assertEqual(by_tag["ndc"], [nueva, vieja])

    def test_country_is_compared_without_accents(self):
        doc = self._make_ndc("ndc-peru", region="Peru", age_days=1)

        by_tag = country_instrument_documents(self.owner, "Perú", ["ndc"])

        self.assertEqual(by_tag["ndc"], [doc])

    def test_untagged_documents_are_not_instruments(self):
        doc = self._make_ndc("ndc-sin-etiqueta", age_days=1)
        doc.evidence_tags.clear()

        by_tag = country_instrument_documents(self.owner, "Colombia", ["ndc"])

        self.assertEqual(by_tag["ndc"], [])
