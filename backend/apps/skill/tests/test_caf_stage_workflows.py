"""
Los workflows del ciclo de una operación CAF, tal como los siembran las
migraciones, y la etapa de cada corrida.

La interfaz lee estos workflows por estructura —el número de cada paso, la
clave de cada columna—, así que lo que se verifica acá es esa estructura: que
exista, que sus tablas cumplan el contrato tabular y que las migraciones no la
dupliquen si vuelven a correr.
"""
import importlib
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument
from apps.project.services.stages import GUIADO_SLUG, IET_SLUG, INSUMO_SLUG, LECTURA_SLUG
from apps.skill.models import Skill, SkillExecution
from apps.skill.table_schema import normalize_table_schema

User = get_user_model()


def _migration(name):
    return importlib.import_module(f"apps.skill.migrations.{name}")


class SeededWorkflowsTestCase(TestCase):
    def _titles(self, slug):
        return list(Skill.objects.get(slug=slug).steps.order_by("position").values_list("title", flat=True))

    def test_every_table_honours_the_table_contract(self):
        for slug in (INSUMO_SLUG, IET_SLUG, GUIADO_SLUG, LECTURA_SLUG):
            for step in Skill.objects.get(slug=slug).steps.filter(output_mode="table"):
                normalize_table_schema(step.table_schema)  # levanta si no cumple

    def test_the_insumo_is_six_steps_and_never_determines(self):
        titles = self._titles(INSUMO_SLUG)
        self.assertEqual([t.split(" ")[0] for t in titles], ["1.", "2.", "3.", "4.", "5.", "6."])
        skill = Skill.objects.get(slug=INSUMO_SLUG)
        self.assertIn("No determinás la alineación", skill.system_prompt)
        objetivo_1 = skill.steps.get(position=4).table_schema
        self.assertEqual(
            [c["key"] for c in objetivo_1["columns"]],
            ["criterio", "puntos_clave", "informacion_faltante"],
        )
        self.assertEqual(len(objetivo_1["columns"][0]["allowed_values"]), 3)
        self.assertEqual(len(skill.steps.get(position=5).table_schema["columns"][0]["allowed_values"]), 5)

    def test_the_iet_ends_with_one_synthesis_that_reads_the_d_steps(self):
        iet = Skill.objects.get(slug=IET_SLUG)
        synthesis = iet.steps.filter(title__startswith="S.")
        self.assertEqual(synthesis.count(), 1)
        step = synthesis.get()
        self.assertEqual(step.position, iet.steps.order_by("-position").first().position)
        self.assertEqual(step.evidence_mode, "previous_steps")
        d_positions = list(iet.steps.filter(title__startswith="D.").values_list("position", flat=True))
        self.assertEqual(sorted(step.history_positions), sorted(d_positions))
        values = step.table_schema["columns"][1]["allowed_values"]
        self.assertIn("No determinable con la información disponible", values)

    def test_the_guided_report_copies_the_iet_and_takes_the_executive_criteria(self):
        guided = Skill.objects.get(slug=GUIADO_SLUG)
        iet_titles = [t for t in self._titles(IET_SLUG) if not t.startswith("S.")]
        self.assertEqual(self._titles(GUIADO_SLUG), iet_titles)
        self.assertEqual(list(guided.parameters.values_list("key", flat=True)), ["criterio_ejecutivo"])
        for step in guided.steps.all():
            has_block = "criterio_ejecutivo" in step.instructions
            self.assertEqual(has_block, step.title.startswith(("D.", "E.")), step.title)

    def test_the_reading_runs_on_a_document(self):
        lectura = Skill.objects.get(slug=LECTURA_SLUG)
        self.assertEqual(lectura.allowed_contexts, ["document"])
        self.assertEqual([t.split(" ")[0] for t in self._titles(LECTURA_SLUG)], ["1.", "2.", "3."])

    def test_running_the_seeds_again_duplicates_nothing(self):
        counts = {slug: Skill.objects.get(slug=slug).steps.count() for slug in (INSUMO_SLUG, IET_SLUG, GUIADO_SLUG, LECTURA_SLUG)}
        _migration("0033_seed_caf_insumo_workflow").create_insumo_workflow(django_apps, None)
        _migration("0034_iet_sintesis_por_criterio").add_sintesis(django_apps, None)
        _migration("0035_seed_caf_iet_guiado_workflow").create_guiado_workflow(django_apps, None)
        _migration("0036_seed_caf_lectura_informe_workflow").create_lectura_workflow(django_apps, None)
        for slug, count in counts.items():
            self.assertEqual(Skill.objects.filter(slug=slug).count(), 1)
            self.assertEqual(Skill.objects.get(slug=slug).steps.count(), count, slug)


class RunCarriesTheStageTestCase(APITestCase):
    def test_a_run_on_an_operation_records_its_stage(self):
        admin = User.objects.create_user(email="admin@example.com", password="x", is_superuser=True)
        project = Project.objects.create(owner=admin, name="Operación", context_notes={"estado": "dec"})
        doc = Document.objects.create(owner=admin, name="IDO", slug="ido", chunking_status="done", extracted_text="x")
        ProjectDocument.objects.create(project=project, document=doc)
        skill = Skill.objects.get(slug=INSUMO_SLUG)
        project.enabled_skills.add(skill)

        self.client.force_authenticate(admin)
        with patch("apps.skill.dispatch.run_skill_task.delay"):
            response = self.client.post(
                reverse("skill-run", kwargs={"slug": skill.slug}),
                {"context_type": "project", "context_slug": project.slug},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(response.data["stage"], "dec")
        self.assertEqual(response.data["skill_slug"], INSUMO_SLUG)
        self.assertEqual(SkillExecution.objects.get(pk=response.data["id"]).stage, "dec")


class SummaryCountsTestCase(APITestCase):
    def test_the_synthesis_is_not_counted_as_a_report_section(self):
        admin = User.objects.create_user(email="admin@example.com", password="x", is_superuser=True)
        project = Project.objects.create(owner=admin, name="Operación")
        SkillExecution.objects.create(
            skill=Skill.objects.get(slug=IET_SLUG), owner=admin, project=project, status="completed",
            output_structured={"steps": [
                {"step_id": 1, "title": "B.1. Marco", "content": "x"},
                {"step_id": 2, "title": "D.2. CT A1 — Riesgos", "content": "y"},
                {"step_id": 3, "title": "S.1. Síntesis por criterio", "content": ""},
            ]},
        )
        self.client.force_authenticate(admin)
        response = self.client.get(f"/api/skills/executions/?project={project.slug}&view=summary")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        self.assertEqual(rows[0]["sections_count"], 2)
