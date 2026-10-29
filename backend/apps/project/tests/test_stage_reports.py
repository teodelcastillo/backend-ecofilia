"""
El informe del ejecutivo de cada etapa.

- Editado: es el reporte de Ecofilia con las ediciones del ejecutivo.
- Subido: el documento del ejecutivo no entra al expediente; se lee apenas se
  puede leer.
- Guiado: Ecofilia reescribe, con el criterio del ejecutivo (que viaja como
  parámetro, con el de Ecofilia al lado), sólo las secciones C, D y E donde
  difieren. Lo reescrito entra como borradores sobre su reporte, con las fuentes
  al pie. Hasta tres generaciones por etapa, salvo el staff.
- Las fotos de la mirada del ejecutivo (alineación y enverdecimiento) y los
  documentos del expediente forman la historia de la operación.
- Emitido, no cambia más: ni la vía, ni el documento. Anularlo es del staff.
- La carpeta institucional muestra los emitidos a toda la organización.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase, APITransactionTestCase

from apps.document.models import Document
from apps.project.models import (
    Project,
    ProjectDocument,
    ProjectShareRole,
    ProjectStageReport,
)
from apps.project.models import ProjectAlignmentSnapshot
from apps.project.services.stage_reports import GUIADO_MAX_PER_STAGE, run_followups_for_document
from apps.project.services.stages import (
    GUIADO_SLUG,
    IET_SLUG,
    LECTURA_SLUG,
    on_execution_finished,
)
from apps.skill.models import ExecutionSectionEdit, ExecutionStatus, Skill, SkillExecution
from apps.user.models import Organization
from apps.user.tests.fixtures import caf_config

User = get_user_model()
DELAY = "apps.skill.dispatch.run_skill_task.delay"


class _ReportBase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        self.owner = User.objects.create_user(email="owner@caf.com", password="x", organization=self.caf)
        self.editor = User.objects.create_user(email="editor@caf.com", password="x", organization=self.caf)
        self.staff = User.objects.create_user(email="staff@example.com", password="x", is_staff=True)
        self.project = Project.objects.create(
            owner=self.owner,
            organization=self.caf,
            name="Operación",
            context_notes={"estado": "ido_cci", "pais": "Perú"},
        )
        self.project.shares.create(user=self.editor, role=ProjectShareRole.EDITOR)
        self.blueprint = Document.objects.create(
            owner=self.owner, name="Documento de la operación", slug="doc-op",
            chunking_status="done", extracted_text="x",
        )
        ProjectDocument.objects.create(project=self.project, document=self.blueprint, is_primary=True)
        self.project.blueprint_document = self.blueprint
        self.project.save(update_fields=["blueprint_document"])
        self.iet_run = SkillExecution.objects.create(
            skill=Skill.objects.get(slug=IET_SLUG), owner=self.owner, project=self.project,
            status=ExecutionStatus.COMPLETED, stage="ido_cci", finished_at=timezone.now(),
            output_structured={"steps": [
                {"step_id": 101, "title": "C.1. Actividades elegibles para financiamiento verde y co-beneficios",
                 "content": "Texto de Ecofilia sobre C."},
                {"step_id": 102, "title": "D.2. CT A1 — Análisis de riesgos climáticos",
                 "content": "Texto de Ecofilia sobre A1."},
                {"step_id": 103, "title": "D.2. CT A2 — Análisis de medidas",
                 "content": "Texto de Ecofilia sobre A2."},
                {"step_id": 105, "title": "E.1. Análisis preliminar de alineación",
                 "content": "Texto de Ecofilia sobre E."},
                {"step_id": 104, "title": "S.1. Síntesis por criterio", "content": "", "table": {"rows": [
                    {"criterio": "CT A1 — Análisis de riesgos climáticos",
                     "determinacion": "Riesgo bajo", "fundamento": "El APRC no muestra amenazas altas."},
                    {"criterio": "Objetivo 1 — Desarrollo resiliente al clima",
                     "determinacion": "Con brechas por CT A2", "fundamento": "Faltan medidas."},
                ]}},
            ]},
        )

    def _post(self, user, path, payload=None):
        self.client.force_authenticate(user)
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/projects/{self.project.slug}/{path}", payload or {}, format="json"
            )
        return response, delay

    def _active(self):
        return ProjectStageReport.objects.get(project=self.project, voided_at__isnull=True)

    def _determine(self):
        self.project.context_notes = {
            **self.project.context_notes,
            "alineacion_paris": {"ct_a1": "Riesgo medio", "objetivo_1": "Con brechas por CT A2"},
            "alineacion_fundamentos": {"ct_a1": "El APRC de CESAS lo califica medio."},
            "alineacion_insumo": {"ct_a1": {"estado": "considerado"}},
            "consideraciones_adicionales": "Capacidad del ejecutor.",
        }
        self.project.save(update_fields=["context_notes"])


class EditadoTestCase(_ReportBase):
    def test_the_executive_report_is_ecofilia_report_with_edits(self):
        response, _ = self._post(self.editor, "stage-report/", {"origin": "editado"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        report = self._active()
        self.assertEqual((report.origin, report.execution, report.stage), ("editado", self.iet_run, "ido_cci"))
        self.assertEqual(response.data["stage_reports"][0]["origin"], "editado")

    def test_without_an_ecofilia_report_there_is_nothing_to_edit(self):
        self.iet_run.delete()
        response, _ = self._post(self.editor, "stage-report/", {"origin": "editado"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SubidoTestCase(_ReportBase):
    def _upload(self, chunking_status="done"):
        return Document.objects.create(
            owner=self.editor, name="Mi IDO", slug="mi-ido", chunking_status=chunking_status,
            extracted_text="x" if chunking_status == "done" else "",
        )

    def test_the_uploaded_report_stays_out_of_the_file_and_is_read(self):
        doc = self._upload()
        response, delay = self._post(self.editor, "stage-report/", {"origin": "subido", "document_slug": doc.slug})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        report = self._active()
        self.assertEqual((report.origin, report.document), ("subido", doc))
        self.assertFalse(ProjectDocument.objects.filter(project=self.project, document=doc).exists())
        reader = report.reader_execution
        self.assertEqual(reader.skill.slug, LECTURA_SLUG)
        self.assertEqual((reader.document, reader.project, reader.stage), (doc, None, "ido_cci"))
        delay.assert_called_once_with(reader.id)

    def test_a_document_from_the_file_cannot_be_the_report(self):
        response, _ = self._post(
            self.owner, "stage-report/", {"origin": "subido", "document_slug": self.blueprint.slug}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_someone_elses_document_cannot_be_the_report(self):
        doc = self._upload()
        response, _ = self._post(self.owner, "stage-report/", {"origin": "subido", "document_slug": doc.slug})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_reading_waits_for_the_document_to_be_processed(self):
        doc = self._upload(chunking_status="processing")
        self._post(self.editor, "stage-report/", {"origin": "subido", "document_slug": doc.slug})
        self.assertIsNone(self._active().reader_execution)

        Document.objects.filter(pk=doc.pk).update(chunking_status="done")
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            run_followups_for_document(doc.id)
        report = self._active()
        self.assertIsNotNone(report.reader_execution)
        delay.assert_called_once_with(report.reader_execution_id)


class GuiadoTestCase(_ReportBase):
    def test_ecofilia_writes_with_the_executive_criteria(self):
        self._determine()
        response, delay = self._post(self.editor, "stage-report/guiado/")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        report = self._active()
        run = report.guided_execution
        # El informe es el reporte de Ecofilia: el guiado escribe borradores sobre él.
        self.assertEqual((report.origin, report.execution), ("guiado", self.iet_run))
        self.assertEqual((run.skill.slug, run.stage), (GUIADO_SLUG, "ido_cci"))
        criterio = run.input_values["criterio_ejecutivo"]
        self.assertIn("CT A1 — Análisis de riesgos climáticos: Riesgo medio.", criterio)
        self.assertIn("El APRC de CESAS lo califica medio.", criterio)
        self.assertIn("lo tuvo en cuenta", criterio)
        self.assertIn("Capacidad del ejecutor.", criterio)
        # Al lado, lo que concluyó Ecofilia y si coinciden.
        self.assertIn("Ecofilia concluyó: Riesgo bajo (El APRC no muestra amenazas altas.). → DIFIERE.", criterio)
        self.assertIn("Ecofilia concluyó: Con brechas por CT A2 (Faltan medidas.). → coincide.", criterio)
        delay.assert_called_once_with(run.id)
        self.assertEqual(response.data["guiado_runs"], {"used": 1, "max": GUIADO_MAX_PER_STAGE})
        self.assertEqual(response.data["stage_reports"][0]["guided_execution_id"], run.id)
        # Queda el criterio con el que se generó, para detectar si cambia después.
        base = self._active().guided_result["base"]
        self.assertEqual(base["determinaciones"]["ct_a1"], "Riesgo medio")
        self.assertEqual(base["consideraciones_adicionales"], "Capacidad del ejecutor.")

    def test_the_guided_report_needs_ecofilia_report(self):
        self._determine()
        self.iet_run.delete()
        response, delay = self._post(self.editor, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        delay.assert_not_called()

    def test_the_greening_alone_is_enough_and_is_compared_with_s2_of_the_iet(self):
        # Lo que sugiere Ecofilia sale de la síntesis S.2 de su propia IET.
        self.iet_run.output_structured["steps"].append(
            {"step_id": 106, "title": "S.2. Componentes con potencial de financiamiento verde", "content": "",
             "table": {"rows": [
                 {"componente": "Planta potabilizadora[[c:1]]", "objetivo": "Adaptación", "monto_usd": 12000000,
                  "fundamento": "Reduce el riesgo hídrico."},
                 {"componente": "Caminos rurales", "objetivo": "Adaptación", "monto_usd": 5000000,
                  "fundamento": "Acceso en lluvias."},
             ]}},
        )
        self.iet_run.save(update_fields=["output_structured"])
        self.project.context_notes = {
            **self.project.context_notes,
            "enverdecimiento": {
                "componentes": [
                    {"componente": "Planta potabilizadora", "objetivo": "Adaptación",
                     "monto_usd": 12000000, "origen": "insumo", "estado": "incluido"},
                    {"componente": "Caminos rurales", "origen": "insumo", "estado": "descartado",
                     "fundamento": "No tienen diseño resiliente."},
                ],
                "oportunidades": [],
                "actualizado_en": "2026-10-19T10:00:00Z",
            },
        }
        self.project.save(update_fields=["context_notes"])

        response, _ = self._post(self.editor, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        criterio = self._active().guided_execution.input_values["criterio_ejecutivo"]
        self.assertIn("Enverdecimiento según el ejecutivo (→ DIFIERE del insumo)", criterio)
        self.assertIn("Descartado por el ejecutivo: Caminos rurales. Por qué: No tienen diseño resiliente.", criterio)
        self.assertIn("Lo que sugería el insumo de Ecofilia: Planta potabilizadora (Adaptación, USD 12,000,000)", criterio)
        self.assertNotIn("[[c:1]]", criterio)

    def test_ecofilia_objetivo_1_follows_the_rules_in_the_criterio(self):
        """La prosa de S.1 dijo «Alineada», pero sus CT dicen otra cosa: al
        guiado le llega lo que sale de las reglas."""
        self.iet_run.output_structured["steps"][-1]["table"]["rows"] = [
            {"criterio": "CT A1 — Análisis de riesgos climáticos", "determinacion": "Riesgo medio", "fundamento": "x"},
            {"criterio": "CT A2 — Análisis de medidas", "determinacion": "No suficientes", "fundamento": "x"},
            {"criterio": "CT A3 — Consistencia con los instrumentos nacionales de adaptación",
             "determinacion": "Consistencia", "fundamento": "x"},
            {"criterio": "Objetivo 1 — Desarrollo resiliente al clima", "determinacion": "Alineada", "fundamento": "x"},
        ]
        self.iet_run.save(update_fields=["output_structured"])
        self.project.context_notes = {
            **self.project.context_notes,
            "alineacion_paris": {"ct_a1": "Riesgo medio", "ct_a2": "No suficientes", "ct_a3": "Consistencia"},
        }
        self.project.save(update_fields=["context_notes"])

        response, _ = self._post(self.editor, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        criterio = self._active().guided_execution.input_values["criterio_ejecutivo"]
        self.assertIn("Objetivo 1 — Desarrollo resiliente al clima: Con brechas por CT A2.", criterio)
        self.assertIn("Ecofilia concluyó: Con brechas por CT A2", criterio)
        self.assertIn("→ coincide.", criterio)
        self.assertNotIn("Ecofilia concluyó: Alineada", criterio)

    def test_without_determinations_there_is_nothing_to_guide(self):
        response, delay = self._post(self.editor, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        delay.assert_not_called()

    def test_the_executive_has_a_limit_and_staff_does_not(self):
        self._determine()
        skill = Skill.objects.get(slug=GUIADO_SLUG)
        for _ in range(GUIADO_MAX_PER_STAGE):
            SkillExecution.objects.create(
                skill=skill, owner=self.editor, project=self.project,
                status=ExecutionStatus.COMPLETED, stage="ido_cci",
            )
        response, delay = self._post(self.editor, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        delay.assert_not_called()

        response, delay = self._post(self.staff, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        delay.assert_called_once()

    def test_the_guided_report_is_not_offered_in_dec(self):
        self._determine()
        self.project.context_notes = {**self.project.context_notes, "estado": "dec"}
        self.project.save(update_fields=["context_notes"])
        response, _ = self._post(self.editor, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GuidedDraftsTestCase(_ReportBase):
    """Al terminar el guiado, lo que cambió entra como borrador sobre el reporte
    de Ecofilia; lo que no, queda como estaba."""

    def _generate(self):
        self._determine()
        response, _ = self._post(self.editor, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return self._active().guided_execution

    def _finish(self, run, steps):
        run.status = ExecutionStatus.COMPLETED
        run.output_structured = {"steps": steps}
        run.save(update_fields=["status", "output_structured"])
        on_execution_finished(run)

    def test_only_what_changed_becomes_a_draft_with_its_sources(self):
        run = self._generate()
        self._finish(run, [
            {"step_id": 1, "title": "C.1. Actividades elegibles para financiamiento verde y co-beneficios",
             "content": "SIN CAMBIOS"},
            {"step_id": 2, "title": "D.2. CT A1 — Análisis de riesgos climáticos",
             "content": "## D.2. CT A1 — Análisis de riesgos climáticos\nEl riesgo es medio según el APRC.",
             "citations": [
                 {"document_name": "Documento de la operación", "page_start": 4, "page_end": 4},
                 {"document_name": "Documento de la operación", "page_start": 7, "page_end": 8},
             ]},
            {"step_id": 3, "title": "D.2. CT A2 — Análisis de medidas", "content": "«Sin cambios»."},
            # Título distinto del de la IET y código repetido: no se adivina.
            {"step_id": 4, "title": "D.2. CT A3 — Consistencia", "content": "Texto nuevo de A3."},
            # Título distinto, pero código único: se ubica por código.
            {"step_id": 5, "title": "E.1. Análisis de alineación", "content": "Texto nuevo de E."},
        ])

        edits = ExecutionSectionEdit.objects.filter(execution=self.iet_run).order_by("step_id")
        self.assertEqual(list(edits.values_list("step_id", flat=True)), [102, 105])
        edit = edits.get(step_id=102)
        self.assertEqual(
            edit.draft,
            "El riesgo es medio según el APRC.\n\n**Fuentes:** Documento de la operación (p. 4, 7–8)",
        )
        # No se publica solo: el ejecutivo lo revisa en el Editor.
        self.assertEqual(edit.published, "")
        result = self._active().guided_result
        self.assertEqual(
            result["cambiadas"],
            [
                {"titulo": "D.2. CT A1 — Análisis de riesgos climáticos", "step_id": 102},
                {"titulo": "E.1. Análisis preliminar de alineación", "step_id": 105},
            ],
        )
        self.assertEqual(len(result["sin_cambios"]), 2)
        self.assertEqual(result["sin_destino"], ["D.2. CT A3 — Consistencia"])
        # Aplicar los borradores no pierde el criterio con que se generaron.
        self.assertEqual(result["base"]["determinaciones"]["ct_a1"], "Riesgo medio")
        self.assertTrue(result["aplicado_en"])

    def test_when_everything_coincides_nothing_is_touched(self):
        run = self._generate()
        self._finish(run, [
            {"step_id": 1, "title": "D.2. CT A1 — Análisis de riesgos climáticos", "content": "SIN CAMBIOS"},
        ])
        self.assertFalse(ExecutionSectionEdit.objects.filter(execution=self.iet_run).exists())
        self.assertEqual(self._active().guided_result["cambiadas"], [])

    def test_an_emitted_report_is_not_touched(self):
        run = self._generate()
        self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET"})
        self._finish(run, [
            {"step_id": 2, "title": "D.2. CT A1 — Análisis de riesgos climáticos", "content": "Otro texto."},
        ])
        self.assertFalse(ExecutionSectionEdit.objects.filter(execution=self.iet_run).exists())


class HistoryTestCase(_ReportBase):
    def _patch_notes(self, notes):
        self.client.force_authenticate(self.editor)
        response = self.client.patch(
            f"/api/projects/{self.project.slug}/",
            {"context_notes": {**self.project.context_notes, **notes}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project.refresh_from_db()

    def test_a_change_in_the_greening_is_a_snapshot(self):
        green = {
            "componentes": [{"componente": "Planta", "objetivo": "Adaptación", "monto_usd": 1, "estado": "incluido"}],
            "oportunidades": [],
        }
        self._patch_notes({"enverdecimiento": {**green, "actualizado_en": "2026-10-19T10:00:00Z"}})
        # Volver a guardar lo mismo, con otra fecha, no es un cambio.
        self._patch_notes({"enverdecimiento": {**green, "actualizado_en": "2026-10-19T11:00:00Z"}})

        snapshot = ProjectAlignmentSnapshot.objects.get(project=self.project)
        self.assertEqual(snapshot.enverdecimiento, green)
        self.assertEqual(snapshot.alineacion, {})
        # Sin determinación no cuenta para la evolución de la alineación.
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get("/api/projects/alignment-history/").data, [])

    def test_the_history_of_the_operation(self):
        self._patch_notes({"alineacion_paris": {"ct_a1": "Riesgo medio"}})
        self.client.force_authenticate(self.editor)
        data = self.client.get(f"/api/projects/{self.project.slug}/history/").data

        self.assertEqual([s["alineacion"] for s in data["snapshots"]], [{"ct_a1": "Riesgo medio"}])
        self.assertEqual(data["snapshots"][0]["captured_by_email"], "editor@caf.com")
        self.assertEqual(data["snapshots"][0]["estado"], "ido_cci")
        self.assertEqual([d["slug"] for d in data["documents"]], ["doc-op"])
        self.assertTrue(data["documents"][0]["is_blueprint"])

        outsider = User.objects.create_user(email="x@otro.com", password="x")
        self.client.force_authenticate(outsider)
        self.assertEqual(
            self.client.get(f"/api/projects/{self.project.slug}/history/").status_code,
            status.HTTP_404_NOT_FOUND,
        )


class EmitTestCase(_ReportBase):
    def test_emitting_freezes_the_report(self):
        self._determine()
        response, _ = self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET\n\nContenido"})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        report = self._active()
        self.assertTrue(report.is_emitted)
        # Sin vía elegida, lo emitido es el reporte de Ecofilia con sus ediciones.
        self.assertEqual((report.origin, report.execution), ("editado", self.iet_run))
        self.assertEqual(report.emitted_markdown, "# IET\n\nContenido")
        self.assertEqual(report.emitted_by, self.editor)
        self.assertEqual(report.emitted_assessment["determinaciones"]["ct_a1"], "Riesgo medio")

    def test_emitting_records_the_objetivo_1_that_follows_from_the_cts(self):
        self.project.context_notes = {
            **self.project.context_notes,
            "alineacion_paris": {
                "ct_a1": "Riesgo bajo", "ct_a2": "No suficientes", "ct_a3": "Consistencia",
                "objetivo_1": "Con brechas por CT A2",
            },
        }
        self.project.save(update_fields=["context_notes"])
        response, _ = self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        assessment = self._active().emitted_assessment
        self.assertEqual(assessment["determinaciones"]["objetivo_1"], "Alineada")
        self.assertEqual(assessment["determinaciones"]["ct_a2"], "No aplica")
        self.assertTrue(assessment["objetivo_1_motivo"])

        response, _ = self._post(self.editor, "stage-report/emitir/", {"markdown": "otro"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, _ = self._post(self.editor, "stage-report/", {"origin": "editado"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, _ = self._post(self.editor, "stage-report/guiado/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_report_in_a_run_needs_its_content(self):
        response, _ = self._post(self.editor, "stage-report/emitir/", {"markdown": "  "})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_emitted_document_cannot_be_changed_or_deleted(self):
        doc = Document.objects.create(
            owner=self.editor, name="Mi IDO", slug="mi-ido", chunking_status="done", extracted_text="x"
        )
        self._post(self.editor, "stage-report/", {"origin": "subido", "document_slug": doc.slug})
        response, _ = self._post(self.editor, "stage-report/emitir/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.editor)
        self.assertEqual(
            self.client.patch(f"/api/document/{doc.slug}/", {"name": "otro"}, format="json").status_code,
            status.HTTP_409_CONFLICT,
        )
        self.assertEqual(self.client.delete(f"/api/document/{doc.slug}/").status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Document.objects.filter(pk=doc.pk).exists())

    def test_only_staff_voids_and_the_record_stays(self):
        self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET"})
        report = self._active()

        response, _ = self._post(self.editor, f"stage-report/{report.id}/anular/", {"reason": "error"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response, _ = self._post(self.staff, f"stage-report/{report.id}/anular/", {"reason": "Versión equivocada"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        report.refresh_from_db()
        self.assertEqual((report.void_reason, report.voided_by), ("Versión equivocada", self.staff))

        # Anulado, la etapa admite un informe nuevo.
        response, _ = self._post(self.editor, "stage-report/", {"origin": "editado"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(ProjectStageReport.objects.filter(project=self.project).count(), 2)


class InstitutionalFolderTestCase(_ReportBase):
    def test_every_member_of_the_institution_reads_the_emitted_reports(self):
        self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET"})
        colleague = User.objects.create_user(email="colega@caf.com", password="x", organization=self.caf)
        outsider = User.objects.create_user(
            email="otro@otro.com", password="x",
            organization=Organization.objects.create(name="Otro", slug="otro"),
        )

        self.client.force_authenticate(colleague)
        listed = self.client.get("/api/projects/stage-reports/emitidos/?organization=caf").data
        self.assertEqual([item["project_slug"] for item in listed], [self.project.slug])
        self.assertEqual(listed[0]["emitted_markdown"], "# IET")
        self.assertEqual(listed[0]["project_pais"], "Perú")
        # Lee el informe, pero la operación no es suya: no se la ofrece abrir.
        self.assertFalse(listed[0]["can_open_project"])
        self.client.force_authenticate(self.owner)
        own = self.client.get("/api/projects/stage-reports/emitidos/?organization=caf").data
        self.assertTrue(own[0]["can_open_project"])

        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get("/api/projects/stage-reports/emitidos/").data, [])

    def test_drafts_are_not_in_the_folder(self):
        self._post(self.editor, "stage-report/", {"origin": "editado"})
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.get("/api/projects/stage-reports/emitidos/").data, [])


class GuiadoDispatchFailureTestCase(APITransactionTestCase):
    """Sin transacción alrededor, como en producción: la corrida se encola en el
    acto y, si no llega a la cola, el pedido lo sabe."""

    def test_a_generation_that_cannot_be_queued_changes_nothing(self):
        # Un TransactionTestCase vacía la base al terminar: el workflow guiado
        # se arma acá en vez de depender de lo que sembró la migración.
        skill, _ = Skill.objects.get_or_create(
            slug=GUIADO_SLUG,
            defaults={"name": "IET guiada", "skill_type": "copilot", "allowed_contexts": ["project"]},
        )
        skill.parameters.get_or_create(
            key="criterio_ejecutivo",
            defaults={"label": "Criterio del ejecutivo", "param_type": "text", "required": True},
        )
        iet, _ = Skill.objects.get_or_create(
            slug=IET_SLUG,
            defaults={"name": "IET", "skill_type": "copilot", "allowed_contexts": ["project"]},
        )
        caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        owner = User.objects.create_user(email="owner@caf.com", password="x", organization=caf)
        project = Project.objects.create(
            owner=owner, organization=caf, name="Operación",
            context_notes={
                "estado": "ido_cci",
                "alineacion_paris": {"ct_a1": "Riesgo medio"},
                "alineacion_fundamentos": {"ct_a1": "APRC de CESAS."},
            },
        )
        doc = Document.objects.create(
            owner=owner, name="Documento", slug="doc-op", chunking_status="done", extracted_text="x"
        )
        ProjectDocument.objects.create(project=project, document=doc, is_primary=True)
        project.blueprint_document = doc
        project.save(update_fields=["blueprint_document"])
        SkillExecution.objects.create(
            skill=iet, owner=owner, project=project, status=ExecutionStatus.COMPLETED,
            stage="ido_cci", finished_at=timezone.now(), output_structured={"steps": []},
        )

        self.client.force_authenticate(owner)
        with patch(DELAY, side_effect=RuntimeError("SQS caído")):
            response = self.client.post(f"/api/projects/{project.slug}/stage-report/guiado/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertFalse(ProjectStageReport.objects.filter(project=project).exists())
        # La corrida que no arrancó no cuenta para el tope: se puede reintentar.
        detail = self.client.get(f"/api/projects/{project.slug}/").data
        self.assertEqual(detail["guiado_runs"]["used"], 0)
