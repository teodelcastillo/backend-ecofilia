"""
El informe del ejecutivo de cada etapa.

- Editado: es el reporte de Ecofilia con las ediciones del ejecutivo.
- Subido: el documento del ejecutivo no entra al expediente; se lee apenas se
  puede leer.
- Guiado: lo que determinó el ejecutivo se plasma tal cual, sin IA, en las
  secciones C, D y E. Sin tope de generaciones: hasta emitir, cada cambio de su
  criterio puede llegar al informe. Cada etapa tiene su reporte (en CAF, DEC
  tiene el suyo).
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
from apps.project.services.stage_reports import run_followups_for_document
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
    SECCIONES = [
        {"titulo": "C.1. Actividades elegibles para financiamiento verde y co-beneficios", "contenido": "Verde del ejecutivo."},
        {"titulo": "D.2. CT A1 — Análisis de riesgos climáticos", "contenido": "**Riesgo medio**\n\n**Fundamento:** El APRC."},
        # Título distinto de la IET y código repetido: no se adivina.
        {"titulo": "D.2. CT A3 — Consistencia", "contenido": "Texto de A3."},
        # Título distinto, pero código único: se ubica por código.
        {"titulo": "E.1. Análisis de alineación", "contenido": "Resumen del ejecutivo."},
    ]

    def _generar(self, secciones=None):
        return self._post(self.editor, "stage-report/guiado/", {"secciones": self.SECCIONES if secciones is None else secciones})

    def test_plasma_lo_del_ejecutivo_sin_lanzar_ia(self):
        self._determine()
        response, delay = self._generar()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        delay.assert_not_called()
        self.assertFalse(SkillExecution.objects.filter(skill__slug=GUIADO_SLUG).exists())
        report = self._active()
        self.assertEqual((report.origin, report.execution, report.guided_execution), ("guiado", self.iet_run, None))
        edit = ExecutionSectionEdit.objects.get(execution=self.iet_run, step_id=102)
        self.assertEqual(edit.published, "**Riesgo medio**\n\n**Fundamento:** El APRC.")
        self.assertEqual(edit.draft, edit.published)
        # Lo de Ecofilia queda intacto debajo.
        self.iet_run.refresh_from_db()
        self.assertEqual(self.iet_run.output_structured["steps"][1]["content"], "Texto de Ecofilia sobre A1.")
        result = report.guided_result
        self.assertEqual([c["step_id"] for c in result["cambiadas"]], [101, 102, 105])
        self.assertEqual(result["sin_destino"], ["D.2. CT A3 — Consistencia"])
        self.assertTrue(result["aplicado_en"])
        # Queda el criterio con el que se generó, para detectar si cambia después.
        self.assertEqual(result["base"]["determinaciones"]["ct_a1"], "Riesgo medio")
        self.assertEqual(result["base"]["consideraciones_adicionales"], "Capacidad del ejecutor.")

    def test_se_puede_regenerar_y_pisa_lo_anterior(self):
        self._determine()
        self._generar()
        response, _ = self._generar([{"titulo": "D.2. CT A1 — Análisis de riesgos climáticos", "contenido": "Otro."}])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        edit = ExecutionSectionEdit.objects.get(execution=self.iet_run, step_id=102)
        self.assertEqual(edit.published, "Otro.")
        self.assertEqual(edit.versions.count(), 2)

    def test_necesita_el_reporte_de_ecofilia(self):
        self._determine()
        self.iet_run.delete()
        response, _ = self._generar()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sin_determinaciones_no_hay_nada_que_plasmar(self):
        response, _ = self._generar()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sin_contenido_no_toca_nada(self):
        self._determine()
        response, _ = self._generar([])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ExecutionSectionEdit.objects.filter(execution=self.iet_run).exists())

    def test_un_informe_emitido_no_se_regenera(self):
        self._determine()
        self._generar()
        self._post(self.editor, "stage-report/emitir/", {"markdown": "# IET"})
        response, _ = self._generar([{"titulo": "D.2. CT A1 — Análisis de riesgos climáticos", "contenido": "Otro."}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotEqual(ExecutionSectionEdit.objects.get(execution=self.iet_run, step_id=102).published, "Otro.")

    def test_no_se_ofrece_en_dec_sin_informe(self):
        self._determine()
        self.project.context_notes = {**self.project.context_notes, "estado": "dec"}
        self.project.save(update_fields=["context_notes"])
        response, _ = self._generar()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CorridaGuiadaEnCursoTestCase(_ReportBase):
    """Una corrida guiada que ya estaba en marcha se sigue aplicando al terminar."""

    def _corrida(self):
        self._determine()
        run = SkillExecution.objects.create(
            skill=Skill.objects.get(slug=GUIADO_SLUG), owner=self.editor, project=self.project,
            status=ExecutionStatus.RUNNING, stage="ido_cci",
        )
        ProjectStageReport.objects.create(
            project=self.project, stage="ido_cci", origin="guiado", execution=self.iet_run,
            guided_execution=run, created_by=self.editor, guided_result={"base": {}},
        )
        return run

    def _finish(self, run, steps):
        run.status = ExecutionStatus.COMPLETED
        run.output_structured = {"steps": steps}
        run.save(update_fields=["status", "output_structured"])
        on_execution_finished(run)

    def test_publica_lo_que_escribio(self):
        self._finish(self._corrida(), [
            {"step_id": 2, "title": "D.2. CT A1 — Análisis de riesgos climáticos", "content": "Texto de la corrida."},
        ])
        self.assertEqual(
            ExecutionSectionEdit.objects.get(execution=self.iet_run, step_id=102).published, "Texto de la corrida."
        )

    def test_regenerar_sin_ia_la_deja_sin_efecto(self):
        run = self._corrida()
        response, _ = self._post(self.editor, "stage-report/guiado/", {"secciones": [
            {"titulo": "D.2. CT A1 — Análisis de riesgos climáticos", "contenido": "Del ejecutivo."},
        ]})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self._finish(run, [
            {"step_id": 2, "title": "D.2. CT A1 — Análisis de riesgos climáticos", "content": "Texto de la corrida."},
        ])
        self.assertEqual(
            ExecutionSectionEdit.objects.get(execution=self.iet_run, step_id=102).published, "Del ejecutivo."
        )


class DecReportTestCase(_ReportBase):
    """DEC tiene su propio informe de Ecofilia: el guiado y el editado viven ahí."""

    DEC_SLUG = "dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion"

    def setUp(self):
        super().setUp()
        self.dec_skill = Skill.objects.create(
            name="IET DEC", slug=self.DEC_SLUG, skill_type=Skill.objects.get(slug=IET_SLUG).skill_type,
            allowed_contexts=["project"], scope="organization", organization=self.caf,
        )
        config = caf_config()
        for stage in config["stages"]:
            if stage["slug"] == "dec":
                stage["report"] = self.DEC_SLUG
                stage["launches"] = [self.DEC_SLUG]
        self.caf.config = config
        self.caf.save(update_fields=["config"])
        self.project.context_notes = {**self.project.context_notes, "estado": "dec"}
        self.project.save(update_fields=["context_notes"])
        self.dec_run = SkillExecution.objects.create(
            skill=self.dec_skill, owner=self.owner, project=self.project,
            status=ExecutionStatus.COMPLETED, stage="dec", finished_at=timezone.now(),
            output_structured={"steps": [
                {"step_id": 202, "title": "D.2. CT A1 — Análisis de riesgos climáticos",
                 "content": "Texto de DEC sobre A1."},
            ]},
        )

    def test_the_dec_report_is_its_own_workflow(self):
        from apps.project.services.stage_reports import current_ecofilia_report

        self.assertEqual(current_ecofilia_report(self.project, "dec"), self.dec_run)
        self.assertEqual(current_ecofilia_report(self.project, "ido_cci"), self.iet_run)

    def test_the_guided_report_writes_on_the_dec_report(self):
        self._determine()
        response, _ = self._post(self.editor, "stage-report/guiado/", {"secciones": [
            {"titulo": "D.2. CT A1 — Análisis de riesgos climáticos", "contenido": "Riesgo medio en DEC."},
        ]})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        report = self._active()
        self.assertEqual((report.stage, report.execution), ("dec", self.dec_run))
        edit = ExecutionSectionEdit.objects.get(execution=self.dec_run, step_id=202)
        self.assertEqual(edit.published, "Riesgo medio en DEC.")


class HistoricalReadingTestCase(APITestCase):
    """Un IET hecho por fuera: lo que lee la lectura pasa a la operación."""

    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        self.staff = User.objects.create_user(email="staff@example.com", password="x", is_staff=True, is_superuser=True)
        self.doc = Document.objects.create(
            owner=self.staff, name="IET DEC externo", slug="iet-dec-externo",
            chunking_status="done", extracted_text="x",
        )

    def test_an_external_dec_report_reaches_the_summary(self):
        self.client.force_authenticate(self.staff)
        with patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/api/projects/historical-report/", {
                "document_slug": self.doc.slug, "name": "Op DEC", "pais": "Perú",
                "organization": "caf", "stage": "dec", "monto": "40",
            }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        project = Project.objects.get(name="Op DEC")
        self.assertEqual(project.context_notes["estado"], "dec")
        report = ProjectStageReport.objects.get(project=project)
        self.assertEqual(report.stage, "dec")
        reader = report.reader_execution
        reader.status = ExecutionStatus.COMPLETED
        reader.output_structured = {"steps": [
            {"title": "1. Determinaciones por criterio", "table": {"rows": [
                {"criterio": "CT A1 — Análisis de riesgos climáticos", "determinacion": "riesgo medio",
                 "fundamento": "El APRC lo dice."},
                {"criterio": "Objetivo 2 — Desarrollo bajo en emisiones de GEI",
                 "determinacion": "No consta en el informe", "fundamento": ""},
            ]}},
            {"title": "3. Consideraciones adicionales", "content": "Capacidad del ejecutor."},
            {"title": "4. Componentes con potencial de financiamiento verde", "table": {"rows": [
                {"componente": "Planta", "codigo_actividad": "A.1.1.3", "actividad_elegible": "Conexiones",
                 "objetivo": "Adaptación", "monto_usd": 10000000, "fundamento": "Reduce pérdidas."},
            ]}},
        ]}
        reader.save(update_fields=["status", "output_structured"])
        on_execution_finished(reader)

        project.refresh_from_db()
        notes = project.context_notes
        self.assertEqual(notes["alineacion_paris"]["ct_a1"], "Riesgo medio")
        self.assertEqual(notes["alineacion_fundamentos"]["ct_a1"], "El APRC lo dice.")
        self.assertEqual(notes["consideraciones_adicionales"], "Capacidad del ejecutor.")
        componente = notes["enverdecimiento"]["componentes"][0]
        self.assertEqual((componente["monto_usd"], componente["estado"]), (10000000.0, "incluido"))
        report.refresh_from_db()
        self.assertTrue(report.is_emitted)
        self.assertEqual(report.emitted_assessment["determinaciones"]["ct_a1"], "Riesgo medio")

    def test_an_unknown_stage_is_rejected(self):
        self.client.force_authenticate(self.staff)
        response = self.client.post("/api/projects/historical-report/", {
            "document_slug": self.doc.slug, "name": "Op X", "pais": "Perú",
            "organization": "caf", "stage": "aprobada",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


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
