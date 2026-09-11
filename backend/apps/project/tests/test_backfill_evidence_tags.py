from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument

User = get_user_model()


class BackfillEvidenceTagsCommandTestCase(TestCase):
    """
    El comando existe para recuperar terreno cuando el matching mejora
    después de que una operación ya se migró: sin él, los vínculos que quedaron
    sin etiqueta en el backfill original (migración 0010) nunca se vuelven a
    evaluar.
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
        self.link = ProjectDocument.objects.create(project=self.project, document=self.doc)

    def test_tags_an_untagged_link(self):
        out = StringIO()
        call_command("backfill_evidence_tags", stdout=out)
        self.link.refresh_from_db()
        self.assertEqual(list(self.link.tags.values_list("slug", flat=True)), ["ndc"])
        self.assertIn("1 de 1", out.getvalue())

    def test_dry_run_does_not_write(self):
        out = StringIO()
        call_command("backfill_evidence_tags", "--dry-run", stdout=out)
        self.assertEqual(self.link.tags.count(), 0)
        self.assertIn("Dry-run", out.getvalue())

    def test_does_not_overwrite_an_existing_tag(self):
        """Nunca pisa una decisión manual, tenga o no matching hoy."""
        otra, _ = EvidenceTag.objects.update_or_create(
            slug="nap", defaults={"name": "NAP", "source_topics": ["naps"]}
        )
        self.link.tags.set([otra])
        out = StringIO()
        call_command("backfill_evidence_tags", stdout=out)
        self.assertEqual(
            list(self.link.tags.values_list("slug", flat=True)), ["nap"]
        )
        self.assertIn("0 de 0", out.getvalue())

    def test_scoped_to_a_single_project(self):
        other_project = Project.objects.create(owner=self.user, name="Otra")
        other_link = ProjectDocument.objects.create(
            project=other_project, document=self.doc
        )
        call_command("backfill_evidence_tags", "--project-slug", other_project.slug)
        other_link.refresh_from_db()
        self.link.refresh_from_db()
        self.assertEqual(other_link.tags.count(), 1)
        self.assertEqual(self.link.tags.count(), 0)
