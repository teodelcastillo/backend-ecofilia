"""
El informe del ejecutivo de cada etapa.

- Editado: es el reporte de Ecofilia con las ediciones del ejecutivo.
- Subido: el documento del ejecutivo no entra al expediente; se lee apenas se
  puede leer.
- Guiado: Ecofilia redacta con el criterio del ejecutivo, que viaja como
  parámetro. Hasta tres generaciones por etapa, salvo el staff.
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
from apps.project.services.stage_reports import GUIADO_MAX_PER_STAGE, run_followups_for_document
from apps.project.services.stages import GUIADO_SLUG, IET_SLUG, LECTURA_SLUG
from apps.skill.models import ExecutionStatus, Skill, SkillExecution
from apps.user.models import Organization

User = get_user_model()
DELAY = "apps.skill.dispatch.run_skill_task.delay"


class _ReportBase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf")
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
            output_structured={"steps": []},
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
        run = report.execution
        self.assertEqual((report.origin, run.skill.slug, run.stage), ("guiado", GUIADO_SLUG, "ido_cci"))
        criterio = run.input_values["criterio_ejecutivo"]
        self.assertIn("CT A1 — Análisis de riesgos climáticos: Riesgo medio.", criterio)
        self.assertIn("El APRC de CESAS lo califica medio.", criterio)
        self.assertIn("lo tuvo en cuenta", criterio)
        self.assertIn("Capacidad del ejecutor.", criterio)
        delay.assert_called_once_with(run.id)
        self.assertEqual(response.data["guiado_runs"], {"used": 1, "max": GUIADO_MAX_PER_STAGE})

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
        caf = Organization.objects.create(name="CAF", slug="caf")
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

        self.client.force_authenticate(owner)
        with patch(DELAY, side_effect=RuntimeError("SQS caído")):
            response = self.client.post(f"/api/projects/{project.slug}/stage-report/guiado/", {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertFalse(ProjectStageReport.objects.filter(project=project).exists())
        # La corrida que no arrancó no cuenta para el tope: se puede reintentar.
        detail = self.client.get(f"/api/projects/{project.slug}/").data
        self.assertEqual(detail["guiado_runs"]["used"], 0)
