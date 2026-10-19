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
from apps.skill.insumo_guide import build_insumo_guide_block
from apps.skill.models import ExecutionStatus, Skill, SkillExecution
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

    def test_the_insumo_is_eight_steps_and_never_determines(self):
        titles = self._titles(INSUMO_SLUG)
        self.assertEqual(
            [t.split(" ")[0] for t in titles], ["1.", "2.", "3.", "4.", "5.", "6.", "7.", "8."]
        )
        self.assertIn("Elementos clave para el análisis de la operación", titles[2])
        skill = Skill.objects.get(slug=INSUMO_SLUG)
        self.assertIn("No determinás la alineación", skill.system_prompt)
        componentes = skill.steps.get(position=4).table_schema
        self.assertEqual(
            [c["key"] for c in componentes["columns"]],
            ["componente", "objetivo", "categoria", "monto_usd", "confianza", "justificacion"],
        )
        oportunidades = skill.steps.get(position=5).table_schema
        self.assertIn("meta_pais", [c["key"] for c in oportunidades["columns"]])
        objetivo_1 = skill.steps.get(position=6).table_schema
        self.assertEqual(
            [c["key"] for c in objetivo_1["columns"]],
            ["criterio", "puntos_clave", "informacion_faltante", "alerta"],
        )
        self.assertEqual(len(objetivo_1["columns"][0]["allowed_values"]), 3)
        self.assertEqual(
            objetivo_1["columns"][3]["allowed_values"],
            ["Brecha posible", "Falta de información", "Sin alertas"],
        )
        self.assertEqual(len(skill.steps.get(position=7).table_schema["columns"][0]["allowed_values"]), 5)

    def test_the_iet_drafts_section_c_after_the_b_steps(self):
        prefixes = [t.split(".")[0] for t in self._titles(IET_SLUG)]
        c_index = prefixes.index("C")
        self.assertEqual(prefixes.count("C"), 1)
        self.assertTrue(all(p == "B" for p in prefixes[:c_index]))
        self.assertEqual(prefixes[c_index + 1], "D")

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

    def test_caf_runs_have_a_document_cap_and_synthesis_reads_previous_sections(self):
        # El insumo tiene tope; el informe (IET y guiado) no, por calidad.
        self.assertEqual(Skill.objects.get(slug=INSUMO_SLUG).max_document_tokens, 300_000)
        for slug in (IET_SLUG, GUIADO_SLUG):
            self.assertIsNone(Skill.objects.get(slug=slug).max_document_tokens, slug)
        iet = Skill.objects.get(slug=IET_SLUG)
        sintesis = [
            s for s in iet.steps.all()
            if "Determinación y recomendaciones" in s.title or s.title.startswith(("E.1.", "E.3."))
        ]
        self.assertEqual(len(sintesis), 4)
        self.assertTrue(all(s.evidence_mode == "previous_steps" for s in sintesis))
        # Los análisis por criterio siguen leyendo documentos.
        self.assertEqual(iet.steps.get(title__startswith="D.2. CT A1").evidence_mode, "both")

    def test_each_synthesis_step_sees_in_full_the_sections_it_integrates(self):
        """Con historia automática verían completas sólo las dos secciones
        anteriores y el resto resumido: sin documentos, eso es perder análisis."""
        iet = Skill.objects.get(slug=IET_SLUG)
        steps = list(iet.steps.order_by("position"))

        def positions(predicate):
            return [s.position for s in steps if predicate(s.title)]

        def step(predicate):
            return next(s for s in steps if predicate(s.title))

        det_1 = step(lambda t: t.startswith("D.2. Determinación"))
        det_2 = step(lambda t: t.startswith("D.3. Determinación"))
        e1 = step(lambda t: t.startswith("E.1."))
        e3 = step(lambda t: t.startswith("E.3."))
        for synthesis in (det_1, det_2, e1, e3):
            self.assertEqual(synthesis.history_mode, "selected", synthesis.title)
        self.assertEqual(det_1.history_positions, positions(lambda t: t.startswith("D.2. CT")))
        self.assertEqual(det_2.history_positions, positions(lambda t: t.startswith("D.3. CT")))
        self.assertEqual(e1.history_positions, positions(lambda t: t.startswith("D.")))
        self.assertEqual(
            e3.history_positions,
            positions(lambda t: t.startswith(("B.5.", "C.1.", "E.1.")) or "Determinación" in t),
        )

    def test_the_guided_report_rewrites_only_greening_and_alignment(self):
        guided = Skill.objects.get(slug=GUIADO_SLUG)
        iet_titles = [t for t in self._titles(IET_SLUG) if t.startswith(("C.", "D.", "E."))]
        self.assertEqual(self._titles(GUIADO_SLUG), iet_titles)
        self.assertEqual(list(guided.parameters.values_list("key", flat=True)), ["criterio_ejecutivo"])
        for step in guided.steps.all():
            self.assertIn("criterio_ejecutivo", step.instructions, step.title)
            self.assertIn("SIN CAMBIOS", step.instructions, step.title)
            for position in step.history_positions or []:
                self.assertTrue(guided.steps.filter(position=position).exists(), step.title)

    def test_the_guided_summary_is_rewritten_whenever_something_differs(self):
        guided = Skill.objects.get(slug=GUIADO_SLUG)
        for step in guided.steps.all():
            resumen = step.title.startswith(("E.1.", "E.3."))
            self.assertEqual("no respondas «SIN CAMBIOS»" in step.instructions, resumen, step.title)

    def test_the_reading_runs_on_a_document(self):
        lectura = Skill.objects.get(slug=LECTURA_SLUG)
        self.assertEqual(lectura.allowed_contexts, ["document"])
        self.assertEqual([t.split(" ")[0] for t in self._titles(LECTURA_SLUG)], ["1.", "2.", "3."])

    def test_running_the_seeds_again_duplicates_nothing(self):
        counts = {slug: Skill.objects.get(slug=slug).steps.count() for slug in (INSUMO_SLUG, IET_SLUG, GUIADO_SLUG, LECTURA_SLUG)}
        _migration("0033_seed_caf_insumo_workflow").create_insumo_workflow(django_apps, None)
        _migration("0034_iet_sintesis_por_criterio").add_c_and_sintesis(django_apps, None)
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


class InsumoGuidesTheReportTestCase(TestCase):
    """El insumo completado de la etapa va como guía a la IET y al guiado, y a
    ningún otro agente."""

    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="x")
        self.project = Project.objects.create(
            owner=self.owner, name="Operación", context_notes={"estado": "ido_cci"}
        )
        SkillExecution.objects.create(
            skill=Skill.objects.get(slug=INSUMO_SLUG), owner=self.owner, project=self.project,
            status=ExecutionStatus.COMPLETED, stage="ido_cci",
            output_structured={"steps": [
                {"step_id": 1, "title": "3. Elementos clave para el análisis de la operación",
                 "content": "### 3. Elementos clave\nLa NDC prioriza agua potable."},
                {"step_id": 2, "title": "4. Componentes con potencial de financiamiento verde",
                 "content": "", "table": {"rows": [
                     {"componente": "Planta potabilizadora", "objetivo": "Adaptación",
                      "monto_usd": 12000000, "confianza": "Alta", "justificacion": "Reduce riesgo hídrico."},
                 ]}},
                {"step_id": 3, "title": "6. Objetivo 1 — puntos clave por criterio", "content": "",
                 "table": {"rows": [
                     {"criterio": "CT A1 — Análisis de riesgos climáticos", "puntos_clave": "Hay APRC.",
                      "informacion_faltante": "Nada relevante", "alerta": "Sin alertas"},
                 ]}},
                {"step_id": 4, "title": "8. Lo primero a mirar", "content": "Mirá el APRC."},
            ]},
        )

    def _block(self, slug, stage="ido_cci"):
        run = SkillExecution(
            skill=Skill.objects.get(slug=slug), owner=self.owner, project=self.project, stage=stage
        )
        return build_insumo_guide_block(run)

    def test_the_report_and_the_guided_report_read_the_insumo(self):
        for slug in (IET_SLUG, GUIADO_SLUG):
            block = self._block(slug)
            self.assertIn("Insumo de Ecofilia", block, slug)
            self.assertIn("La NDC prioriza agua potable.", block)
            self.assertIn("Planta potabilizadora", block)
            self.assertIn("[Sin alertas]", block)
            self.assertIn("Mirá el APRC.", block)
            # El encabezado del paso no se repite.
            self.assertNotIn("### 3. Elementos clave\n", block)

    def test_no_other_agent_and_no_other_stage(self):
        self.assertEqual(self._block(INSUMO_SLUG), "")
        self.assertEqual(self._block(LECTURA_SLUG), "")
        self.assertEqual(self._block(IET_SLUG, stage="dec"), "")
