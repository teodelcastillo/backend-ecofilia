from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument

User = get_user_model()


class BackfillEvidenceTagsCommandTestCase(TestCase):
    """
    El comando etiqueta lo que se cargó sin etiqueta a partir de sus temas.
    Como las operaciones heredan las etiquetas del documento, etiquetar acá
    alcanza para todas las operaciones que lo tienen vinculado.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="op@example.com", password="secret123", username="op"
        )
        self.project = Project.objects.create(owner=self.user, name="Operación")
        EvidenceTag.objects.update_or_create(
            slug="ndc", defaults={"name": "NDC", "source_topics": ["ndcs", "ndc"]}
        )
        self.doc = Document.objects.create(
            owner=self.user, name="NDC suelto", slug="ndc-suelto",
            topics=["ndc colombia 2023"],
        )
        ProjectDocument.objects.create(project=self.project, document=self.doc)

    def test_tags_an_untagged_document(self):
        out = StringIO()
        call_command("backfill_evidence_tags", stdout=out)
        self.assertEqual(list(self.doc.evidence_tags.values_list("slug", flat=True)), ["ndc"])
        self.assertIn("1 de 1", out.getvalue())

    def test_dry_run_does_not_write(self):
        out = StringIO()
        call_command("backfill_evidence_tags", "--dry-run", stdout=out)
        self.assertEqual(self.doc.evidence_tags.count(), 0)
        self.assertIn("Dry-run", out.getvalue())

    def test_does_not_overwrite_an_existing_tag(self):
        """Nunca pisa una decisión de quien cargó el documento."""
        otra, _ = EvidenceTag.objects.update_or_create(
            slug="nap", defaults={"name": "NAP", "source_topics": ["naps"]}
        )
        self.doc.evidence_tags.set([otra])
        out = StringIO()
        call_command("backfill_evidence_tags", stdout=out)
        self.assertEqual(list(self.doc.evidence_tags.values_list("slug", flat=True)), ["nap"])
        self.assertIn("0 de 0", out.getvalue())

    def test_scoped_to_a_single_project(self):
        suelto = Document.objects.create(
            owner=self.user, name="Otra NDC", slug="otra-ndc", topics=["ndcs"]
        )
        call_command("backfill_evidence_tags", "--project-slug", self.project.slug, stdout=StringIO())
        self.assertEqual(self.doc.evidence_tags.count(), 1)
        self.assertEqual(suelto.evidence_tags.count(), 0)
