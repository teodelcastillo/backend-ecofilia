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


class ExecutionStepListTestCase(APITestCase):
    """``?view=step``: una sección de cada corrida, sin traer el informe."""

    def setUp(self):
        self.user = User.objects.create_user(email="s@example.com", password="x", username="s")
        self.project = Project.objects.create(owner=self.user, name="Op")
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"]
        )
        self.execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.COMPLETED,
            output="# Informe\n" + "texto " * 5000,
            output_structured={
                "steps": [
                    {"title": "B.1. Marco", "content": "y " * 5000, "citations": [_cita(True)]},
                    {
                        "title": "S.2. Componentes",
                        "table": {"rows": [{"componente": "Vivienda", "monto_usd": 3500000}]},
                        "citations": [_cita(True)],
                    },
                ]
            },
            metadata={"sources": [{"document_slug": "d"}] * 500},
        )
        self.client.force_authenticate(self.user)
        self.url = reverse("skill-execution-list")

    def _row(self, **params):
        response = self.client.get(self.url, {"view": "step", "step": "S.2", **params})
        self.assertEqual(response.status_code, 200)
        return next(r for r in response.json() if r["id"] == self.execution.pk)

    def test_trae_solo_la_seccion_pedida(self):
        steps = self._row()["output_structured"]["steps"]
        self.assertEqual([s["title"] for s in steps], ["S.2. Componentes"])
        self.assertEqual(steps[0]["table"]["rows"][0]["monto_usd"], 3500000)

    def test_deja_afuera_lo_pesado(self):
        row = self._row()
        self.assertNotIn("citations", row["output_structured"]["steps"][0])
        for field in ("output", "edited_output", "metadata"):
            self.assertNotIn(field, row)

    def test_conserva_los_datos_del_resumen(self):
        row = self._row()
        self.assertEqual(row["sections_count"], 1)
        self.assertEqual(row["status"], ExecutionStatus.COMPLETED)
        self.assertEqual(row["project_slug"], self.project.slug)

    def test_corrida_sin_esa_seccion_viene_vacia(self):
        self.assertEqual(self._row(step="Z.9")["output_structured"], {"steps": []})

    def test_el_prefijo_no_se_interpreta_como_consulta(self):
        self.assertEqual(self._row(step="S.2' || '")["output_structured"], {"steps": []})

    def test_sin_seccion_es_un_error(self):
        response = self.client.get(self.url, {"view": "step"})
        self.assertEqual(response.status_code, 400)

    def test_no_abre_corridas_ajenas(self):
        otro = User.objects.create_user(email="z@example.com", password="x", username="z")
        self.client.force_authenticate(otro)
        response = self.client.get(self.url, {"view": "step", "step": "S.2"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])
