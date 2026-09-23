"""
Lanzar una corrida sobre una operación: que no quede en `pending` para siempre
y que no arranque sobre un expediente a medio procesar.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument
from apps.skill.dispatch import dispatch_execution
from apps.skill.models import (
    ExecutionStatus,
    Skill,
    SkillExecution,
    SkillStep,
    SkillType,
    StepEvidenceSelection,
)
from apps.skill.reliability import requeue_pending_executions
from apps.skill.services import SkillRunner

User = get_user_model()


class _Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="e@example.com", password="x", username="e")
        self.project = Project.objects.create(owner=self.user, name="Operación")
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"]
        )

    def _execution(self, **kwargs):
        return SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project, **kwargs
        )


class DispatchTestCase(_Base):
    def test_a_failed_enqueue_leaves_the_run_failed_with_a_reason(self):
        execution = self._execution(status=ExecutionStatus.PENDING)
        with patch("apps.skill.dispatch.run_skill_task.delay", side_effect=RuntimeError("SQS caído")):
            self.assertFalse(dispatch_execution(execution.id))
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.FAILED)
        self.assertIn("SQS caído", execution.error_message)

    def test_dispatch_stamps_the_time(self):
        execution = self._execution(status=ExecutionStatus.PENDING)
        with patch("apps.skill.dispatch.run_skill_task.delay"):
            dispatch_execution(execution.id)
        execution.refresh_from_db()
        self.assertIn("dispatched_at", execution.metadata)


class RequeuePendingTestCase(_Base):
    def _stuck(self, minutes=30, **metadata):
        execution = self._execution(status=ExecutionStatus.PENDING, metadata=metadata)
        SkillExecution.objects.filter(pk=execution.pk).update(
            created_at=timezone.now() - timedelta(minutes=minutes)
        )
        return execution

    def test_old_pending_is_redispatched(self):
        execution = self._stuck()
        with patch("apps.skill.dispatch.run_skill_task.delay") as delay:
            result = requeue_pending_executions(threshold_minutes=10)
        delay.assert_called_once_with(execution.id)
        self.assertEqual(result["requeued"], [execution.id])

    def test_recent_dispatch_is_left_alone(self):
        execution = self._stuck(dispatched_at=timezone.now().isoformat())
        with patch("apps.skill.dispatch.run_skill_task.delay") as delay:
            requeue_pending_executions(threshold_minutes=10)
        delay.assert_not_called()
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.PENDING)

    def test_gives_up_after_the_limit(self):
        execution = self._stuck(auto_requeues=2)
        with patch("apps.skill.dispatch.run_skill_task.delay") as delay:
            result = requeue_pending_executions(threshold_minutes=10)
        delay.assert_not_called()
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.FAILED)
        self.assertEqual(result["failed"], [execution.id])


class AtomicClaimTestCase(_Base):
    def test_a_run_already_claimed_is_not_run_twice(self):
        """El reaper puede reencolar; si el mensaje viejo aparece, no corre dos veces."""
        execution = self._execution(status=ExecutionStatus.PENDING)
        # Este runner leyó `pending`, pero otro worker la tomó en el medio.
        stale = SkillExecution.objects.get(pk=execution.pk)
        SkillExecution.objects.filter(pk=execution.pk).update(status=ExecutionStatus.RUNNING)
        with patch("apps.skill.services.SkillExecution.objects.select_related") as select, patch(
            "apps.skill.services.resolve_documents"
        ) as resolve:
            select.return_value.prefetch_related.return_value.get.return_value = stale
            SkillRunner().run(execution.id)
        resolve.assert_not_called()


class PreflightApiTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="p@example.com", password="x", username="p")
        self.client.force_authenticate(self.user)
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"]
        )
        SkillStep.objects.create(
            skill=self.skill, title="CT M3", instructions="x", position=1,
            evidence_selection=StepEvidenceSelection.TAGGED, evidence_tags=["ndc"],
        )
        EvidenceTag.objects.update_or_create(slug="ndc", defaults={"name": "NDC"})
        self.project = Project.objects.create(owner=self.user, name="Op")
        self.project.enabled_skills.add(self.skill)
        self.doc = Document.objects.create(
            owner=self.user, name="IDO", slug="ido", chunking_status="done", extracted_text="x"
        )
        ProjectDocument.objects.create(project=self.project, document=self.doc)

    def _run(self):
        return self.client.post(
            reverse("skill-run", kwargs={"slug": self.skill.slug}),
            {"context_type": "project", "context_slug": self.project.slug},
            format="json",
        )

    def test_blocks_while_a_document_is_processing(self):
        Document.objects.filter(pk=self.doc.pk).update(chunking_status="processing")
        with patch("apps.skill.dispatch.run_skill_task.delay") as delay:
            response = self._run()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("IDO", response.data["detail"])
        delay.assert_not_called()
        self.assertFalse(SkillExecution.objects.exists())

    def test_warnings_are_recorded_in_the_run(self):
        with patch("apps.skill.dispatch.run_skill_task.delay"):
            response = self._run()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        codes = {w["code"] for w in SkillExecution.objects.get().metadata["preflight_warnings"]}
        self.assertEqual(codes, {"sin_objetivo_componentes", "etiquetas_faltantes"})

    def test_no_warnings_when_the_operation_is_complete(self):
        self.project.context_notes = {"objetivo": "a", "componentes": "b"}
        self.project.save()
        self.doc.evidence_tags.set(EvidenceTag.objects.filter(slug="ndc"))
        with patch("apps.skill.dispatch.run_skill_task.delay"):
            self._run()
        self.assertNotIn("preflight_warnings", SkillExecution.objects.get().metadata)
