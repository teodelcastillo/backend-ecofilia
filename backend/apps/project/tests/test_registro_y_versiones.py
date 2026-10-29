"""
Lo que sumó la devolución de octubre al portal CAF:

- Versión nueva de un informe emitido: el emitido queda como anterior y la
  etapa admite uno vigente nuevo, que parte del mismo informe.
- Registro de evaluación: el análisis IA, el informe emitido del ejecutivo y
  los subidos o históricos con la misma forma, con «No consta» donde no dicen.
- Carga de un IET histórico: sólo el staff; crea la operación con el informe
  como subido y no lanza el análisis.
- Completar criterios: rehacer una corrida terminada desde la síntesis.
- El tipo de operación que completa la IA queda en la lista o no queda.
"""
from unittest.mock import patch

from rest_framework import status

from apps.document.models import Document
from apps.project.models import Project, ProjectStageReport
from apps.project.services.ai_fill import TIPO_SUGERIDO_KEY, extract_fields
from apps.project.services.evaluation_records import NO_CONSTA, key_de_etiqueta, records_for
from apps.project.tests.test_stage_reports import DELAY, _ReportBase
from apps.skill.models import ExecutionStatus, SkillExecution
from apps.skill.services import rerun_from_step


class NuevaVersionTestCase(_ReportBase):
    def _emitir(self):
        self._post(self.editor, "stage-report/", {"origin": "editado"})
        response, _ = self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET v1"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_a_new_version_keeps_the_emitted_one_as_previous(self):
        self._emitir()
        response, _ = self._post(self.editor, "stage-report/nueva-version/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        anterior, nueva = ProjectStageReport.objects.filter(project=self.project).order_by("id")
        self.assertIsNotNone(anterior.superseded_at)
        self.assertTrue(anterior.is_emitted)
        self.assertEqual(anterior.emitted_markdown, "# IET v1")
        self.assertIsNone(nueva.emitted_at)
        self.assertEqual((nueva.origin, nueva.execution, nueva.previous), ("editado", self.iet_run, anterior))

        # La nueva se emite de nuevo y queda como la última emitida.
        response, _ = self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET v2"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        nueva.refresh_from_db()
        self.assertEqual(nueva.emitted_markdown, "# IET v2")

    def test_only_an_emitted_report_admits_a_new_version(self):
        self._post(self.editor, "stage-report/", {"origin": "editado"})
        response, _ = self._post(self.editor, "stage-report/nueva-version/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RegistroTestCase(_ReportBase):
    def test_label_to_key(self):
        self.assertEqual(key_de_etiqueta("CT M4 — Consistencia con trayectorias"), "ct_m4")
        self.assertEqual(key_de_etiqueta("Objetivo 2 — Desarrollo bajo en emisiones"), "objetivo_2")
        self.assertIsNone(key_de_etiqueta("Recomendaciones"))

    def test_ia_and_executive_records_share_the_shape(self):
        self._determine()
        self._post(self.editor, "stage-report/", {"origin": "editado"})
        self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET"})

        registros = {r["origen"]: r for r in records_for([self.project])}
        self.assertEqual(set(registros), {"ia", "ejecutivo"})
        ia, ejecutivo = registros["ia"], registros["ejecutivo"]
        self.assertEqual(set(ia["criterios"]), set(ejecutivo["criterios"]))
        self.assertEqual(ia["criterios"]["ct_a1"]["valor"], "Riesgo bajo")
        self.assertEqual(ejecutivo["criterios"]["ct_a1"]["valor"], "Riesgo medio")
        # Lo que el ejecutivo no determinó no queda vacío: no consta.
        self.assertEqual(ejecutivo["criterios"]["ct_m4"]["valor"], NO_CONSTA)
        self.assertEqual(ia["pais"], "Perú")

    def test_endpoint_lists_the_records(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get("/api/projects/evaluation-records/?organization=caf")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([r["origen"] for r in response.data], ["ia"])


class HistoricoTestCase(_ReportBase):
    def setUp(self):
        super().setUp()
        self.iet_doc = Document.objects.create(
            owner=self.staff, name="IET 2024", slug="iet-2024", chunking_status="pending"
        )

    def _cargar(self, user):
        self.client.force_authenticate(user)
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                "/api/projects/historical-report/",
                {"document_slug": "iet-2024", "name": "Puerto 2024", "pais": "Chile", "fecha_informe": "2024-05-10",
                 "organization": "caf"},
                format="json",
            )
        return response, delay

    def test_staff_loads_a_historical_report_without_running_the_analysis(self):
        response, delay = self._cargar(self.staff)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        project = Project.objects.get(name="Puerto 2024")
        self.assertEqual(project.organization, self.caf)
        self.assertTrue(project.context_notes["historico"])
        report = ProjectStageReport.objects.get(project=project)
        self.assertEqual((report.origin, report.document, report.stage), ("subido", self.iet_doc, "ido_cci"))
        self.assertFalse(SkillExecution.objects.filter(project=project).exists())
        delay.assert_not_called()

        # Sin lectura todavía, entra al registro como externo sin leer.
        registro = records_for([project])[0]
        self.assertEqual((registro["origen"], registro["leido"], registro["anio"]), ("externo", False, 2024))

    def test_only_staff_loads_historical_reports(self):
        response, _ = self._cargar(self.owner)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class CompletarCriteriosTestCase(_ReportBase):
    def test_rerun_from_the_synthesis_keeps_what_came_before(self):
        steps = list(self.iet_run.skill.steps.all())
        index = next(i for i, s in enumerate(steps) if s.title == "S.1. Síntesis por criterio")
        self.iet_run.output_structured = {
            "steps": [{"title": s.title, "content": f"paso {i}"} for i, s in enumerate(steps)]
        }
        self.iet_run.save(update_fields=["output_structured"])

        rerun_from_step(self.iet_run, "S.1. Síntesis por criterio")
        self.iet_run.refresh_from_db()
        self.assertEqual(self.iet_run.status, ExecutionStatus.PENDING)
        self.assertEqual(self.iet_run.steps_completed, index)
        self.assertEqual(len(self.iet_run.output_structured["steps"]), index)

    def test_an_analysis_from_an_older_definition_has_to_be_redone(self):
        # La corrida del fixture no tiene los pasos de la definición de hoy.
        with self.assertRaises(ValueError):
            rerun_from_step(self.iet_run, "S.1. Síntesis por criterio")

    def test_only_a_finished_run(self):
        self.iet_run.status = ExecutionStatus.RUNNING
        self.iet_run.save(update_fields=["status"])
        with self.assertRaises(ValueError):
            rerun_from_step(self.iet_run, "S.1. Síntesis por criterio")


class TipoOperacionTestCase(_ReportBase):
    def test_a_type_outside_the_list_is_dropped(self):
        for respuesta, esperado in (
            ({"tipo_operacion": "Préstamo programático (PBL)"}, "Préstamo programático (PBL)"),
            ({"tipo_operacion": "Préstamo de algo"}, None),
        ):
            with patch("apps.project.services.ai_fill._complete", return_value=(respuesta, {})):
                self.assertEqual(extract_fields("texto", ["tipo_operacion"])["tipo_operacion"], esperado)

    def test_the_filled_type_is_marked_as_suggested(self):
        from apps.project.services.ai_fill import fill_missing_from_blueprint

        resultado = {"objetivo": "Obj", "componentes": "1. C", "tipo_operacion": "Préstamo programático (PBL)"}
        with patch("apps.project.services.ai_fill._complete", return_value=(resultado, {})):
            fill_missing_from_blueprint(self.project.id)
        self.project.refresh_from_db()
        self.assertEqual(self.project.context_notes["tipo_operacion"], "Préstamo programático (PBL)")
        self.assertEqual(self.project.context_notes[TIPO_SUGERIDO_KEY], "ia")


class CatalogoVerdeTestCase(_ReportBase):
    """El catálogo de actividades de CAF llega a C.1 y a S.2 (``skill/0047``)."""

    def test_the_catalog_has_four_levels_and_unique_activity_codes(self):
        from apps.skill.caf_catalogo_verde import catalogo, codigos_de_actividad

        codigos = codigos_de_actividad()
        self.assertIn("B.1.1.1", codigos)
        self.assertNotIn("B.1.1", codigos)
        self.assertEqual(len(codigos), len(set(codigos)))
        self.assertEqual({r["nivel"] for r in catalogo()}, {1, 2, 3, 4})
        # Sin las marcas de nota al pie del documento de CAF.
        self.assertFalse(any("verde6" in r["nombre"] for r in catalogo()))

    def test_s2_asks_for_the_activity_code_with_the_catalog(self):
        from apps.skill.models import SkillStep

        s2 = SkillStep.objects.get(skill__slug="caf-iet-datbc-evaluacion-tecnica", title__startswith="S.2")
        columna = next(c for c in s2.table_schema["columns"] if c["key"] == "codigo_actividad")
        # Texto, no lista cerrada: S.2 es estricto y un código raro no debe tumbar el análisis.
        self.assertEqual((columna["type"], columna["required"]), ("text", False))
        self.assertIn("- M.6.1.1 Recogida selectiva", s2.instructions)
        for c1 in SkillStep.objects.filter(title__startswith="C.1."):
            self.assertIn("Catálogo de actividades elegibles de CAF", c1.instructions)
