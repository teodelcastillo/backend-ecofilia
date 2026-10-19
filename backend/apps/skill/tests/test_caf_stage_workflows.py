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
from apps.skill.models import ExecutionStatus, Skill, SkillExecution
from apps.skill.services import _with_operation_context
from apps.skill.table_schema import normalize_table_schema
from apps.user.models import Organization

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

    def test_section_c_no_longer_points_to_the_insumo(self):
        c1 = Skill.objects.get(slug=IET_SLUG).steps.get(title__startswith="C.1.")
        self.assertNotIn("insumo", c1.instructions.lower())
        self.assertIn("componentes que califican o podrían calificar", c1.instructions)
        self.assertIn("recomendaciones para aumentar los co-beneficios", c1.instructions)
        self.assertIn("Citá el documento de cada dato.", c1.instructions)

    def test_the_iet_ends_with_two_hidden_syntheses(self):
        iet = Skill.objects.get(slug=IET_SLUG)
        steps = list(iet.steps.order_by("position"))
        s1, s2 = [s for s in steps if s.title.startswith("S.")]
        self.assertEqual([s.title[:4] for s in steps[-2:]], ["S.1.", "S.2."])

        # S.1: las conclusiones por criterio, leyendo los pasos D.
        self.assertEqual(s1.evidence_mode, "previous_steps")
        d_positions = [s.position for s in steps if s.title.startswith("D.")]
        self.assertEqual(sorted(s1.history_positions), sorted(d_positions))
        keys = [c["key"] for c in s1.table_schema["columns"]]
        self.assertEqual(keys, ["criterio", "determinacion", "fundamento", "informacion_faltante"])
        values = s1.table_schema["columns"][1]["allowed_values"]
        self.assertIn("No determinable con la información disponible", values)
        self.assertIn("Inconsistente", values)
        self.assertIn("Reglas del Objetivo 1", s1.instructions)
        self.assertIn("Consistencia / Potencialmente inconsistente / Inconsistente", s1.instructions)

        # S.2: los componentes verdes, leyendo sólo C.1.
        c1 = next(s for s in steps if s.title.startswith("C.1."))
        self.assertEqual(s2.title, "S.2. Componentes con potencial de financiamiento verde")
        self.assertEqual((s2.evidence_mode, s2.history_mode), ("previous_steps", "selected"))
        self.assertEqual(s2.history_positions, [c1.position])
        self.assertEqual((s2.evidence_selection, s2.output_validation), ("blueprint_only", "strict"))
        self.assertEqual(
            [c["key"] for c in s2.table_schema["columns"]],
            ["componente", "objetivo", "categoria", "monto_usd", "fundamento"],
        )
        self.assertEqual(
            s2.table_schema["columns"][1]["allowed_values"],
            ["Adaptación", "Mitigación", "Biodiversidad y otros objetivos ambientales"],
        )

    def test_the_iet_reads_the_operation_document_tags(self):
        """Las etiquetas finas de los documentos de la operación llegan a los
        pasos que leían «Documento de la operación» y «Salvaguardas / CESAS»;
        el guiado, que copia la evidencia de la IET, también."""
        for slug in (IET_SLUG, GUIADO_SLUG):
            skill = Skill.objects.get(slug=slug)
            c1 = skill.steps.get(title__startswith="C.1.")
            for tag in ("ias", "analisis-riesgos", "matriz-resultados", "disenos-proyecto"):
                self.assertIn(tag, c1.evidence_tags, slug)
            a1 = skill.steps.get(title__startswith="D.2. CT A1")
            self.assertIn("salvaguardas-cesas", a1.evidence_tags)
            self.assertIn("ias", a1.evidence_tags)
            self.assertIn("analisis-riesgos", a1.evidence_tags)
            # Lo que no leía documentos de la operación no cambia.
            a3 = skill.steps.get(title__startswith="D.2. CT A3")
            self.assertNotIn("ias", a3.evidence_tags)
            self.assertNotIn("matriz-resultados", a3.evidence_tags)

    def test_the_0040_migration_reverses_to_the_0034_state(self):
        migration = _migration("0040_iet_sin_insumo_y_componentes_verdes")
        iet = Skill.objects.get(slug=IET_SLUG)
        count = iet.steps.count()
        migration.revertir(django_apps, None)
        self.assertEqual(iet.steps.count(), count - 1)
        self.assertFalse(iet.steps.filter(title__startswith="S.2.").exists())
        s1 = iet.steps.get(title__startswith="S.1.")
        self.assertEqual([c["key"] for c in s1.table_schema["columns"]], ["criterio", "determinacion", "fundamento"])
        self.assertNotIn("Inconsistente", s1.table_schema["columns"][1]["allowed_values"])
        self.assertNotIn("Reglas del Objetivo 1", s1.instructions)
        self.assertIn("Tomá como guía el insumo de Ecofilia", iet.steps.get(title__startswith="C.1.").instructions)
        migration.aplicar(django_apps, None)
        self.assertEqual(iet.steps.count(), count)

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
        before = {
            s.title: (s.instructions, s.table_schema, s.evidence_tags)
            for s in Skill.objects.get(slug=IET_SLUG).steps.all()
        }
        _migration("0033_seed_caf_insumo_workflow").create_insumo_workflow(django_apps, None)
        _migration("0034_iet_sintesis_por_criterio").add_c_and_sintesis(django_apps, None)
        _migration("0035_seed_caf_iet_guiado_workflow").create_guiado_workflow(django_apps, None)
        _migration("0036_seed_caf_lectura_informe_workflow").create_lectura_workflow(django_apps, None)
        _migration("0040_iet_sin_insumo_y_componentes_verdes").aplicar(django_apps, None)
        _migration("0042_iet_lee_etiquetas_de_operacion").aplicar(django_apps, None)
        for slug, count in counts.items():
            self.assertEqual(Skill.objects.filter(slug=slug).count(), 1)
            self.assertEqual(Skill.objects.get(slug=slug).steps.count(), count, slug)
        after = {
            s.title: (s.instructions, s.table_schema, s.evidence_tags)
            for s in Skill.objects.get(slug=IET_SLUG).steps.all()
        }
        self.assertEqual(after, before)


class InsumoIsNoLongerAnAgentTestCase(TestCase):
    """El insumo (``0033``) queda como historial: nadie lo corre ni lo recibe
    una operación nueva, y la IET no lo lee."""

    def test_the_insumo_is_retired_from_defaults_and_cannot_be_run(self):
        insumo = Skill.objects.get(slug=INSUMO_SLUG)
        self.assertFalse(insumo.members_can_run)
        caf = Organization.objects.create(name="CAF", slug="caf")
        owner = User.objects.create_user(email="owner@caf.com", password="x", organization=caf)
        iet = Skill.objects.get(slug=IET_SLUG)
        caf.enabled_skills.add(iet, insumo)
        caf.default_project_skills.add(iet, insumo)
        project = Project.objects.create(owner=owner, organization=caf, name="Vieja")
        project.enabled_skills.add(iet, insumo)

        _migration("0041_insumo_deja_de_ser_agente").retirar(django_apps, None)

        self.assertTrue(Skill.objects.filter(slug=INSUMO_SLUG).exists())
        self.assertEqual(list(caf.default_project_skills.values_list("slug", flat=True)), [IET_SLUG])
        self.assertEqual(list(project.enabled_skills.values_list("slug", flat=True)), [IET_SLUG])

        _migration("0041_insumo_deja_de_ser_agente").reponer(django_apps, None)
        self.assertIn(INSUMO_SLUG, caf.default_project_skills.values_list("slug", flat=True))
        self.assertIn(INSUMO_SLUG, project.enabled_skills.values_list("slug", flat=True))

    def test_the_report_prompt_carries_no_insumo_block(self):
        owner = User.objects.create_user(email="owner@example.com", password="x")
        project = Project.objects.create(
            owner=owner, name="Operación", context_notes={"estado": "ido_cci", "pais": "Perú"}
        )
        SkillExecution.objects.create(
            skill=Skill.objects.get(slug=INSUMO_SLUG), owner=owner, project=project,
            status=ExecutionStatus.COMPLETED, stage="ido_cci",
            output_structured={"steps": [
                {"step_id": 1, "title": "8. Lo primero a mirar", "content": "Mirá el APRC."},
            ]},
        )
        for slug in (IET_SLUG, GUIADO_SLUG):
            run = SkillExecution(skill=Skill.objects.get(slug=slug), owner=owner, project=project, stage="ido_cci")
            prompt = _with_operation_context("Sos Ecofilia.", run)
            self.assertTrue(prompt.startswith("Sos Ecofilia."))
            self.assertNotIn("Insumo de Ecofilia", prompt)
            self.assertNotIn("Mirá el APRC.", prompt)


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
