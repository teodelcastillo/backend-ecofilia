"""
Las etapas de una operación: quién las mueve y qué lanzan.

- Avanzar es de quien edita; volver atrás, del staff o de quien administra la
  institución.
- Entrar a IDO lanza el reporte de Ecofilia (la IET), una vez por etapa. No hay
  nada que esperar antes: el insumo que ve el ejecutivo es la ficha de esa
  misma corrida. Una IET anterior a las etapas cuenta como de IDO.
- Lo que no se pudo lanzar queda bloqueado y se reintenta solo cuando la
  operación está lista.
- La edición de la operación no cambia la etapa: sólo la acción.
- Una operación avanza con los informes de sus etapas emitidos: sin el de IDO
  no pasa a DEC, y sin el de DEC (ni el de IDO, si pasó por IDO) no queda
  aprobada. Una operación creada de entrada en DEC no pasó por IDO: no se le
  exige.
- DEC tiene su propio informe, que analiza el documento DEC como IDO analizó
  el IDO: mientras el principal sea el de IDO, no corre.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument, ProjectShareRole, ProjectStageTransition
from apps.project.services.stage_reports import run_followups_for_document
from apps.project.services.stages import (
    IET_SLUG,
    INSUMO_SLUG,
    on_execution_finished,
    retry_pending_stage_launches,
    stage_workflow_slugs,
)
from apps.skill.models import ExecutionStatus, Skill, SkillExecution
from apps.user.models import Organization
from apps.user.tests.fixtures import caf_config

User = get_user_model()
DELAY = "apps.skill.dispatch.run_skill_task.delay"


class _StageBase(APITestCase):
    def setUp(self):
        # Una operación de CAF: sus etapas y lo que lanzan salen de la
        # configuración de la institución (fase 1).
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        self.owner = User.objects.create_user(email="owner@example.com", password="x", organization=self.caf)
        # Una operación de CAF se comparte sólo con gente de CAF.
        self.editor = User.objects.create_user(email="editor@example.com", password="x", organization=self.caf)
        self.viewer = User.objects.create_user(email="viewer@example.com", password="x", organization=self.caf)
        # Sin MFA, el staff no tiene alcance de plataforma (ADMIN_REQUIRE_MFA).
        self.staff = User.objects.create_user(
            email="staff@example.com", password="x", is_staff=True, mfa_enabled=True
        )
        self.project = Project.objects.create(
            owner=self.owner,
            name="Operación",
            context_notes={"estado": "pre_originacion", "pais": "Perú"},
        )
        self.project.shares.create(user=self.editor, role=ProjectShareRole.EDITOR)
        self.project.shares.create(user=self.viewer, role=ProjectShareRole.VIEWER)
        self.blueprint = Document.objects.create(
            owner=self.owner, name="Documento de la operación", slug="doc-op",
            chunking_status="done", extracted_text="x",
        )
        ProjectDocument.objects.create(project=self.project, document=self.blueprint, is_primary=True)
        self.project.blueprint_document = self.blueprint
        self.project.save(update_fields=["blueprint_document"])
        self.iet = Skill.objects.get(slug=IET_SLUG)

    def _post(self, user, path, payload=None):
        self.client.force_authenticate(user)
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/projects/{self.project.slug}/{path}", payload or {}, format="json"
            )
        return response, delay

    def _move(self, user, to):
        return self._post(user, "stage/", {"to": to})

    def _emit(self, stage):
        from django.utils import timezone

        from apps.project.models import ProjectStageReport

        ProjectStageReport.objects.create(
            project=self.project, stage=stage, origin="editado", created_by=self.owner,
            emitted_at=timezone.now(), emitted_by=self.owner, emitted_markdown="# IET",
        )

    def _set_stage(self, stage):
        self.project.context_notes = {**self.project.context_notes, "estado": stage}
        self.project.save(update_fields=["context_notes"])

    def _statuses(self, launches):
        return {entry["skill_slug"]: entry["status"] for entry in launches}


class StageTransitionTestCase(_StageBase):
    def test_ido_launches_only_the_report(self):
        self.assertEqual(stage_workflow_slugs(self.project, "ido_cci"), [IET_SLUG])
        self.assertEqual(stage_workflow_slugs(self.project, "dec"), [])

    def test_entering_ido_launches_the_report_at_once(self):
        response, delay = self._move(self.editor, "ido_cci")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["project"]["context_notes"]["estado"], "ido_cci")
        self.assertEqual(self._statuses(response.data["launches"]), {IET_SLUG: "launched"})
        run = SkillExecution.objects.get(project=self.project)
        self.assertEqual((run.skill.slug, run.stage), (IET_SLUG, "ido_cci"))
        self.assertEqual(run.metadata.get("launched_by"), "stage")
        delay.assert_called_once_with(run.id)
        # Nada queda esperando a otro agente.
        self.assertNotIn("waiting", self._statuses(response.data["launches"]).values())
        self.assertFalse(SkillExecution.objects.filter(skill__slug=INSUMO_SLUG).exists())

        transition = ProjectStageTransition.objects.get(project=self.project)
        self.assertEqual((transition.from_stage, transition.to_stage), ("pre_originacion", "ido_cci"))
        self.assertEqual(transition.created_by, self.editor)
        self.assertEqual(response.data["project"]["stage_history"][0]["to_stage"], "ido_cci")

    def test_a_report_from_before_the_stages_counts_as_ido(self):
        legacy = SkillExecution.objects.create(
            skill=self.iet, owner=self.owner, project=self.project,
            status=ExecutionStatus.COMPLETED, stage="",
        )
        response, delay = self._move(self.editor, "ido_cci")

        self.assertEqual(self._statuses(response.data["launches"]), {IET_SLUG: "exists"})
        delay.assert_not_called()
        self.assertEqual(
            SkillExecution.objects.filter(project=self.project, skill=self.iet).get(), legacy
        )

    def test_a_finished_report_launches_nothing_else(self):
        self._move(self.editor, "ido_cci")
        report = SkillExecution.objects.get(project=self.project, skill=self.iet)
        report.status = ExecutionStatus.COMPLETED
        report.save(update_fields=["status"])
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            on_execution_finished(report)
        delay.assert_not_called()
        self.assertEqual(SkillExecution.objects.filter(project=self.project).count(), 1)

    def test_the_launcher_cannot_relaunch_what_the_stage_launched(self):
        self._move(self.editor, "ido_cci")
        run = SkillExecution.objects.filter(project=self.project, skill=self.iet).get()
        self.client.force_authenticate(self.editor)
        response = self.client.post(f"/api/skills/executions/{run.id}/rerun/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_viewer_cannot_move_the_stage(self):
        response, _ = self._move(self.viewer, "ido_cci")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_going_back_is_for_staff_only(self):
        self._set_stage("ido_cci")
        response, _ = self._move(self.editor, "pre_originacion")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response, delay = self._move(self.staff, "pre_originacion")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["launches"], [])
        delay.assert_not_called()

    def test_moving_to_the_same_or_an_unknown_stage_is_rejected(self):
        response, _ = self._move(self.editor, "pre_originacion")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response, _ = self._move(self.editor, "otra")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_detail_says_who_can_revert(self):
        self.client.force_authenticate(self.editor)
        self.assertFalse(self.client.get(f"/api/projects/{self.project.slug}/").data["can_revert_stage"])
        self.client.force_authenticate(self.staff)
        self.assertTrue(self.client.get(f"/api/projects/{self.project.slug}/").data["can_revert_stage"])


class StageEditGuardTestCase(_StageBase):
    def test_editing_the_operation_keeps_the_server_stage(self):
        self.client.force_authenticate(self.editor)
        response = self.client.patch(
            f"/api/projects/{self.project.slug}/",
            {"context_notes": {"estado": "dec", "pais": "Chile"}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.context_notes["estado"], "pre_originacion")
        self.assertEqual(self.project.context_notes["pais"], "Chile")
        self.assertFalse(SkillExecution.objects.filter(project=self.project).exists())

    def test_a_new_operation_starts_in_the_stage_it_is_created_with(self):
        self.client.force_authenticate(self.owner)
        response = self.client.post(
            "/api/projects/",
            {"name": "Otra", "context_notes": {"estado": "ido_cci"}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            Project.objects.get(slug=response.data["slug"]).context_notes["estado"], "ido_cci"
        )


class BlockedLaunchTestCase(_StageBase):
    def test_without_a_principal_document_the_launch_waits(self):
        self.project.blueprint_document = None
        self.project.save(update_fields=["blueprint_document"])

        response, delay = self._move(self.editor, "ido_cci")

        self.assertEqual(self._statuses(response.data["launches"]), {IET_SLUG: "blocked"})
        delay.assert_not_called()
        self.assertFalse(SkillExecution.objects.filter(project=self.project).exists())

        # Elegido el principal, el reintento lanza lo pendiente una sola vez.
        self.project.refresh_from_db()
        self.project.blueprint_document = self.blueprint
        self.project.save(update_fields=["blueprint_document"])
        with patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            retried = retry_pending_stage_launches(self.project)
        self.assertEqual(self._statuses(retried), {IET_SLUG: "launched"})
        self.assertIsNone(retry_pending_stage_launches(self.project))
        self.assertEqual(SkillExecution.objects.filter(project=self.project).count(), 1)

        transition = ProjectStageTransition.objects.get(project=self.project)
        self.assertEqual(self._statuses(transition.launches), {IET_SLUG: "launched"})

    def test_a_document_still_processing_blocks_until_it_finishes(self):
        annex = Document.objects.create(
            owner=self.owner, name="Anexo", slug="anexo", chunking_status="processing"
        )
        ProjectDocument.objects.create(project=self.project, document=annex)

        response, _ = self._move(self.editor, "ido_cci")
        launches = response.data["launches"]
        self.assertEqual(self._statuses(launches), {IET_SLUG: "blocked"})
        self.assertIn("Anexo", launches[0]["reason"])

        Document.objects.filter(pk=annex.pk).update(chunking_status="done")
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            run_followups_for_document(annex.id)
        self.assertEqual(delay.call_count, 1)
        self.assertEqual(
            set(SkillExecution.objects.filter(project=self.project).values_list("stage", flat=True)),
            {"ido_cci"},
        )

    def test_what_was_blocked_in_a_past_stage_is_not_launched(self):
        self.project.blueprint_document = None
        self.project.save(update_fields=["blueprint_document"])
        self._move(self.editor, "ido_cci")
        self._emit("ido_cci")
        self._move(self.editor, "dec")

        self.project.refresh_from_db()
        self.assertEqual(self.project.context_notes["estado"], "dec")
        self.project.blueprint_document = self.blueprint
        self.project.save(update_fields=["blueprint_document"])
        self.assertIsNone(retry_pending_stage_launches(self.project))
        self.assertFalse(SkillExecution.objects.filter(project=self.project).exists())


class DecComoIdoTestCase(_StageBase):
    DEC_SLUG = "dec-iet-datbc-informe-de-evaluacion-tecnica-de-la-operacion"

    def setUp(self):
        super().setUp()
        self.dec = Skill.objects.create(
            name="IET DEC", slug=self.DEC_SLUG, skill_type=self.iet.skill_type,
            allowed_contexts=["project"], scope="organization", organization=self.caf,
        )
        config = caf_config()
        for stage in config["stages"]:
            if stage["slug"] == "dec":
                stage["report"] = self.DEC_SLUG
                stage["launches"] = [self.DEC_SLUG]
        self.caf.config = config
        self.caf.save(update_fields=["config"])
        self.project.organization = self.caf
        self.project.save(update_fields=["organization"])
        self._set_stage("ido_cci")
        # El principal de IDO lleva la etiqueta de su etapa.
        link = ProjectDocument.objects.get(project=self.project, document=self.blueprint)
        link.tags.set(self._tags("ido"))
        link.tags_overridden = True
        link.save(update_fields=["tags_overridden"])

    def _tags(self, *slugs):
        from apps.document.models import EvidenceTag

        from django.db.models import Q

        return EvidenceTag.objects.filter(Q(organization__isnull=True) | Q(organization=self.caf), slug__in=slugs)

    def test_without_the_ido_report_emitted_it_does_not_go_to_dec(self):
        response, delay = self._move(self.editor, "dec")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertIn("emití el informe", str(response.data))
        self.project.refresh_from_db()
        self.assertEqual(self.project.context_notes["estado"], "ido_cci")
        delay.assert_not_called()

    def test_dec_waits_for_its_document_and_then_runs(self):
        self._emit("ido_cci")
        response, delay = self._move(self.editor, "dec")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        launch = response.data["launches"][0]
        self.assertEqual((launch["skill_slug"], launch["status"]), (self.DEC_SLUG, "blocked"))
        self.assertIn("documento DEC", launch["reason"])
        delay.assert_not_called()

        # Sube el DEC y lo marca como principal: se lanza el análisis de DEC.
        doc_dec = Document.objects.create(
            owner=self.owner, name="Documento DEC", slug="doc-dec", chunking_status="done", extracted_text="x",
        )
        link = ProjectDocument.objects.create(project=self.project, document=doc_dec, tags_overridden=True)
        link.tags.set(self._tags("dec"))
        self.project.refresh_from_db()
        self.project.blueprint_document = doc_dec
        self.project.save(update_fields=["blueprint_document"])
        with patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            retried = retry_pending_stage_launches(self.project)
        self.assertEqual(self._statuses(retried), {self.DEC_SLUG: "launched"})
        run = SkillExecution.objects.get(project=self.project, skill=self.dec)
        self.assertEqual(run.stage, "dec")

    def test_approved_needs_every_stage_it_went_through_emitted(self):
        self._emit("ido_cci")
        self._move(self.editor, "dec")
        response, _ = self._move(self.editor, "aprobada")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertIn("DEC", str(response.data))
        self._emit("dec")
        response, _ = self._move(self.editor, "aprobada")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_created_in_dec_only_needs_its_dec_report_to_be_approved(self):
        self._set_stage("dec")
        response, _ = self._move(self.editor, "aprobada")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        self.assertNotIn("IDO", str(response.data))
        self._emit("dec")
        response, _ = self._move(self.editor, "aprobada")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_an_operation_created_in_dec_launches_its_report(self):
        # Creada de entrada en DEC: no pasó por IDO, no se le exige su informe.
        self._set_stage("dec")
        link = ProjectDocument.objects.get(project=self.project, document=self.blueprint)
        link.tags.set(self._tags("dec"))
        response, _ = self._post(self.editor, "stage/launch/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._statuses(response.data["launches"]), {self.DEC_SLUG: "launched"})


class StageLaunchEndpointTestCase(_StageBase):
    def test_an_operation_created_in_ido_launches_on_demand_once(self):
        self._set_stage("ido_cci")
        response, _ = self._post(self.editor, "stage/launch/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._statuses(response.data["launches"]), {IET_SLUG: "launched"})

        response, delay = self._post(self.editor, "stage/launch/")
        self.assertEqual(self._statuses(response.data["launches"]), {IET_SLUG: "exists"})
        delay.assert_not_called()

    def test_a_failed_run_is_left_for_a_superadmin(self):
        self._set_stage("ido_cci")
        SkillExecution.objects.create(
            skill=self.iet, owner=self.owner, project=self.project,
            status=ExecutionStatus.FAILED, stage="ido_cci",
        )
        response, _ = self._post(self.editor, "stage/launch/")
        self.assertEqual(self._statuses(response.data["launches"]), {IET_SLUG: "failed_before"})

    def test_an_operation_created_in_ido_waits_for_processing_and_launches_alone(self):
        """Creada directamente en IDO, con documentos en proceso: el análisis
        queda bloqueado y arranca solo cuando terminan."""
        self._set_stage("ido_cci")
        annex = Document.objects.create(
            owner=self.owner, name="Anexo", slug="anexo-ido", chunking_status="processing"
        )
        ProjectDocument.objects.create(project=self.project, document=annex)
        response, _ = self._post(self.editor, "stage/launch/")
        self.assertEqual(self._statuses(response.data["launches"]), {IET_SLUG: "blocked"})

        Document.objects.filter(pk=annex.pk).update(chunking_status="done")
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            run_followups_for_document(annex.id)
        self.assertEqual(delay.call_count, 1)

    def test_only_a_superadmin_adds_step_instructions(self):
        self._set_stage("ido_cci")
        step = self.iet.steps.order_by("position").first()
        payload = {"step_instructions": {IET_SLUG: {str(step.id): "Prestá atención al riesgo hídrico."}}}
        self._post(self.editor, "stage/launch/", payload)
        run = SkillExecution.objects.get(project=self.project)
        self.assertEqual(run.step_instructions, {})

        run.delete()
        admin = User.objects.create_user(email="root@example.com", password="x", is_superuser=True)
        self.project.shares.create(user=admin, role=ProjectShareRole.EDITOR)
        self._post(admin, "stage/launch/", payload)
        run = SkillExecution.objects.get(project=self.project)
        self.assertEqual(run.step_instructions, {str(step.id): "Prestá atención al riesgo hídrico."})

    def test_step_instructions_survive_a_blocked_launch(self):
        annex = Document.objects.create(
            owner=self.owner, name="Anexo", slug="anexo-instr", chunking_status="processing"
        )
        ProjectDocument.objects.create(project=self.project, document=annex)
        admin = User.objects.create_user(email="root2@example.com", password="x", is_superuser=True)
        self.project.shares.create(user=admin, role=ProjectShareRole.EDITOR)
        step = self.iet.steps.order_by("position").first()
        self._post(
            admin, "stage/", {"to": "ido_cci", "step_instructions": {IET_SLUG: {str(step.id): "Detalle"}}}
        )
        Document.objects.filter(pk=annex.pk).update(chunking_status="done")
        with patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            run_followups_for_document(annex.id)
        run = SkillExecution.objects.get(project=self.project)
        self.assertEqual(run.step_instructions, {str(step.id): "Detalle"})

    def test_a_stage_without_workflows_launches_nothing(self):
        self._set_stage("dec")
        response, delay = self._post(self.editor, "stage/launch/")
        self.assertEqual(response.data["launches"], [])
        delay.assert_not_called()
