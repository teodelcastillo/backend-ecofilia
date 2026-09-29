"""
El listado de corridas en su forma resumida (``?view=summary``).

El listado completo mandaba cada corrida entera y llegó a 12 MB: armar esa
respuesta mataba a los workers de la API por memoria. El resumen deja el
informe en la base y trae sólo lo que las pantallas de listado leen.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework.test import APITestCase

from apps.project.models import Project
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillType

User = get_user_model()


def _cita(verified: bool) -> dict:
    return {"cited_text": "x" * 2000, "verified": verified}


class ExecutionSummaryListTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="o@example.com", password="x", username="o")
        self.project = Project.objects.create(owner=self.user, name="Op")
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"]
        )
        self.original = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.COMPLETED,
            output="# Informe\n" + "texto " * 5000,
            output_structured={
                "steps": [
                    {"title": "I", "content": "a", "citations": [_cita(True), _cita(False)]},
                    {"title": "II", "content": "b", "citations": [_cita(True)]},
                    {"title": "III", "content": "c"},
                ]
            },
            metadata={"usage": {"input_tokens": 1}, "sources": [{"document_slug": "d"}] * 500},
        )
        self.rerun = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.RUNNING,
            metadata={"rerun_of": self.original.pk},
        )
        self.client.force_authenticate(self.user)

    def _list(self, **params):
        response = self.client.get(reverse("skill-execution-list"), params)
        self.assertEqual(response.status_code, 200)
        return {row["id"]: row for row in response.json()}

    def test_summary_leaves_the_report_out(self):
        rows = self._list(view="summary")
        for field in ("output", "output_structured", "edited_output", "metadata"):
            self.assertNotIn(field, rows[self.original.pk])

    def test_summary_counts_sections_and_citations_in_the_database(self):
        row = self._list(view="summary")[self.original.pk]
        self.assertEqual(row["sections_count"], 3)
        self.assertEqual(row["citations_count"], 3)
        self.assertEqual(row["citations_verified_count"], 2)

    def test_run_without_steps_counts_zero(self):
        row = self._list(view="summary")[self.rerun.pk]
        self.assertEqual(row["sections_count"], 0)
        self.assertEqual(row["citations_count"], 0)

    def test_rerun_link_survives_without_metadata(self):
        rows = self._list(view="summary")
        self.assertEqual(rows[self.rerun.pk]["rerun_of"], self.original.pk)
        self.assertIsNone(rows[self.original.pk]["rerun_of"])

    def test_summary_keeps_the_filters(self):
        rows = self._list(view="summary", status=ExecutionStatus.COMPLETED)
        self.assertEqual(list(rows), [self.original.pk])

    def test_default_list_is_unchanged(self):
        """Los clientes que no piden el resumen siguen recibiendo todo."""
        row = self._list()[self.original.pk]
        self.assertIn("output_structured", row)
        self.assertEqual(len(row["output_structured"]["steps"]), 3)
