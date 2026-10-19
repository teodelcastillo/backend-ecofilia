"""
Las etapas de una operación: quién las mueve y qué lanzan.

- Avanzar es de quien edita; volver atrás, del staff o de quien administra la
  institución.
- Entrar a IDO lanza el insumo, una vez por etapa. El reporte de Ecofilia (IET)
  lo espera: se lanza cuando el insumo termina, porque es su guía. Una IET
  anterior a las etapas cuenta como de IDO.
- Lo que no se pudo lanzar queda bloqueado y se reintenta solo cuando la
  operación está lista.
- La edición de la operación no cambia la etapa: sólo la acción.
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
)
from apps.skill.models import ExecutionStatus, Skill, SkillExecution

User = get_user_model()
DELAY = "apps.skill.dispatch.run_skill_task.delay"


class _StageBase(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(email="owner@example.com", password="x")
        self.editor = User.objects.create_user(email="editor@example.com", password="x")
        self.viewer = User.objects.create_user(email="viewer@example.com", password="x")
        self.staff = User.objects.create_user(email="staff@example.com", password="x", is_staff=True)
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
        self.insumo = Skill.objects.get(slug=INSUMO_SLUG)

    def _post(self, user, path, payload=None):
        self.client.force_authenticate(user)
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                f"/api/projects/{self.project.slug}/{path}", payload or {}, format="json"
            )
        return response, delay

    def _move(self, user, to):
        return self._post(user, "stage/", {"to": to})

    def _set_stage(self, stage):
        self.project.context_notes = {**self.project.context_notes, "estado": stage}
        self.project.save(update_fields=["context_notes"])

    def _statuses(self, launches):
        return {entry["skill_slug"]: entry["status"] for entry in launches}


class StageTransitionTestCase(_StageBase):
    def test_entering_ido_launches_the_insumo_and_the_report_waits_for_it(self):
        response, delay = self._move(self.editor, "ido_cci")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["project"]["context_notes"]["estado"], "ido_cci")
        self.assertEqual(
            self._statuses(response.data["launches"]),
            {IET_SLUG: "waiting", INSUMO_SLUG: "launched"},
        )
        run = SkillExecution.objects.get(project=self.project)
        self.assertEqual((run.skill.slug, run.stage), (INSUMO_SLUG, "ido_cci"))
        self.assertEqual(run.metadata.get("launched_by"), "stage")
        self.assertEqual(delay.call_count, 1)

        transition = ProjectStageTransition.objects.get(project=self.project)
        self.assertEqual((transition.from_stage, transition.to_stage), ("pre_originacion", "ido_cci"))
        self.assertEqual(transition.created_by, self.editor)
        self.assertEqual(response.data["project"]["stage_history"][0]["to_stage"], "ido_cci")

    def test_a_report_from_before_the_stages_counts_as_ido(self):
        legacy = SkillExecution.objects.create(
            skill=self.iet, owner=self.owner, project=self.project,
            status=ExecutionStatus.COMPLETED, stage="",
        )
        response, _ = self._move(self.editor, "ido_cci")

        self.assertEqual(
            self._statuses(response.data["launches"]),
            {IET_SLUG: "exists", INSUMO_SLUG: "launched"},
        )
        self.assertEqual(
            SkillExecution.objects.filter(project=self.project, skill=self.iet).get(), legacy
        )

    def test_the_finished_insumo_launches_the_report_once(self):
        self._move(self.editor, "ido_cci")
        insumo = SkillExecution.objects.get(project=self.project, skill=self.insumo)

        insumo.status = ExecutionStatus.FAILED
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            on_execution_finished(insumo)
        delay.assert_not_called()

        insumo.status = ExecutionStatus.COMPLETED
        insumo.save(update_fields=["status"])
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            on_execution_finished(insumo)
            on_execution_finished(insumo)
        report = SkillExecution.objects.get(project=self.project, skill=self.iet)
        self.assertEqual((report.stage, report.metadata.get("launched_by")), ("ido_cci", "stage"))
        delay.assert_called_once_with(report.id)
        transition = ProjectStageTransition.objects.get(project=self.project)
        self.assertEqual(
            self._statuses(transition.launches), {IET_SLUG: "launched", INSUMO_SLUG: "launched"}
        )

    def test_an_insumo_from_a_stage_already_left_launches_nothing(self):
        self._move(self.editor, "ido_cci")
        insumo = SkillExecution.objects.get(project=self.project, skill=self.insumo)
        self._move(self.editor, "dec")
        SkillExecution.objects.filter(pk=insumo.pk).update(status=ExecutionStatus.COMPLETED)
        insumo.refresh_from_db()
        with patch(DELAY) as delay, self.captureOnCommitCallbacks(execute=True):
            on_execution_finished(insumo)
        delay.assert_not_called()
        self.assertFalse(SkillExecution.objects.filter(project=self.project, skill=self.iet).exists())

    def test_the_launcher_cannot_relaunch_what_the_stage_launched(self):
        self._move(self.editor, "ido_cci")
        run = SkillExecution.objects.filter(project=self.project, skill=self.insumo).get()
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

        self.assertEqual(
            self._statuses(response.data["launches"]),
            {IET_SLUG: "waiting", INSUMO_SLUG: "blocked"},
        )
        delay.assert_not_called()
        self.assertFalse(SkillExecution.objects.filter(project=self.project).exists())

        # Elegido el principal, el reintento lanza lo pendiente una sola vez.
        self.project.refresh_from_db()
        self.project.blueprint_document = self.blueprint
        self.project.save(update_fields=["blueprint_document"])
        with patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            retried = retry_pending_stage_launches(self.project)
        self.assertEqual(self._statuses(retried), {INSUMO_SLUG: "launched"})
        self.assertIsNone(retry_pending_stage_launches(self.project))
        self.assertEqual(SkillExecution.objects.filter(project=self.project).count(), 1)

        transition = ProjectStageTransition.objects.get(project=self.project)
        self.assertEqual(
            self._statuses(transition.launches), {IET_SLUG: "waiting", INSUMO_SLUG: "launched"}
        )

    def test_a_document_still_processing_blocks_until_it_finishes(self):
        annex = Document.objects.create(
            owner=self.owner, name="Anexo", slug="anexo", chunking_status="processing"
        )
        ProjectDocument.objects.create(project=self.project, document=annex)

        response, _ = self._move(self.editor, "ido_cci")
        launches = response.data["launches"]
        self.assertEqual(self._statuses(launches), {IET_SLUG: "waiting", INSUMO_SLUG: "blocked"})
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
        self._move(self.editor, "dec")

        self.project.refresh_from_db()
        self.assertEqual(self.project.context_notes["estado"], "dec")
        self.project.blueprint_document = self.blueprint
        self.project.save(update_fields=["blueprint_document"])
        self.assertIsNone(retry_pending_stage_launches(self.project))
        self.assertFalse(SkillExecution.objects.filter(project=self.project).exists())


class StageLaunchEndpointTestCase(_StageBase):
    def test_an_operation_created_in_ido_launches_on_demand_once(self):
        self._set_stage("ido_cci")
        response, _ = self._post(self.editor, "stage/launch/")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self._statuses(response.data["launches"]),
            {IET_SLUG: "waiting", INSUMO_SLUG: "launched"},
        )

        response, delay = self._post(self.editor, "stage/launch/")
        self.assertEqual(
            self._statuses(response.data["launches"]),
            {IET_SLUG: "waiting", INSUMO_SLUG: "exists"},
        )
        delay.assert_not_called()

    def test_a_failed_run_is_left_for_a_superadmin(self):
        self._set_stage("ido_cci")
        SkillExecution.objects.create(
            skill=self.insumo, owner=self.owner, project=self.project,
            status=ExecutionStatus.FAILED, stage="ido_cci",
        )
        response, _ = self._post(self.editor, "stage/launch/")
        self.assertEqual(
            self._statuses(response.data["launches"]),
            {IET_SLUG: "waiting", INSUMO_SLUG: "failed_before"},
        )

    def test_a_stage_without_workflows_launches_nothing(self):
        self._set_stage("dec")
        response, delay = self._post(self.editor, "stage/launch/")
        self.assertEqual(response.data["launches"], [])
        delay.assert_not_called()
