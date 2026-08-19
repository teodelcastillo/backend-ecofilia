"""
Reanudar una ejecución muerta, sin repetir lo que ya completó.

Distinto de "repetir" (`/rerun/`, Fase 7): eso crea una corrida nueva sobre la
definición de hoy, para comparar. Esto es continuar la MISMA fila desde donde
quedó — el caso que hasta ahora sólo se podía resolver a mano, desde un shell
de producción.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.project.models import Project, ProjectShare, ProjectShareRole
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillType
from apps.skill.services import resume_execution

User = get_user_model()


class ResumeExecutionServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="autor@example.com", password="secret123", username="autor",
        )
        self.project = Project.objects.create(owner=self.user, name="Operación")
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
        )

    def _execution(self, status_, **kwargs):
        return SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project,
            status=status_, **kwargs,
        )

    def test_a_stalled_execution_becomes_pending(self):
        execution = self._execution(ExecutionStatus.STALLED, steps_completed=9)
        resumed = resume_execution(execution)

        self.assertEqual(resumed.status, ExecutionStatus.PENDING)
        self.assertIsNotNone(resumed.last_progress_at)

    def test_a_failed_execution_becomes_pending(self):
        execution = self._execution(ExecutionStatus.FAILED)
        resumed = resume_execution(execution)
        self.assertEqual(resumed.status, ExecutionStatus.PENDING)

    def test_a_running_execution_cannot_be_resumed(self):
        """Es la barrera contra la doble escritura: si la ejecución sigue viva
        de verdad, reanudarla igual pondría dos procesos a escribir el mismo
        `output_structured`."""
        execution = self._execution(ExecutionStatus.RUNNING)
        with self.assertRaises(ValueError):
            resume_execution(execution)

    def test_a_completed_execution_cannot_be_resumed(self):
        execution = self._execution(ExecutionStatus.COMPLETED)
        with self.assertRaises(ValueError):
            resume_execution(execution)

    def test_steps_completed_and_output_are_untouched(self):
        """No reconstruye nada: el motor ya sabe saltear lo que
        `output_structured` diga que está completo."""
        execution = self._execution(
            ExecutionStatus.STALLED,
            steps_completed=9,
            output_structured={"steps": [{"step_id": 1}] * 9},
        )
        resumed = resume_execution(execution)

        self.assertEqual(resumed.steps_completed, 9)
        self.assertEqual(len(resumed.output_structured["steps"]), 9)


class ResumeExecutionAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="autor@example.com", password="secret123", username="autor",
        )
        self.other_user = User.objects.create_user(
            email="otro@example.com", password="secret123", username="otro",
        )
        self.client.force_authenticate(self.user)

        self.project = Project.objects.create(owner=self.user, name="Operación")
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
        )
        self.execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project,
            status=ExecutionStatus.STALLED, steps_completed=9,
            error_message="Sin progreso desde hace más de 15 minutos.",
        )

    def _url(self, execution=None):
        return reverse(
            "skill-execution-resume", kwargs={"pk": (execution or self.execution).pk}
        )

    @patch("apps.skill.api.views.run_skill_task")
    def test_resume_dispatches_the_task_and_returns_202(self, mock_task):
        response = self.client.post(self._url(), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(response.data["status"], "pending")
        mock_task.delay.assert_called_once_with(self.execution.id)

    @patch("apps.skill.api.views.run_skill_task")
    def test_resuming_a_quick_skill_runs_synchronously(self, mock_task):
        self.skill.skill_type = SkillType.QUICK
        self.skill.save(update_fields=["skill_type"])

        def synchronous_completion(execution_id):
            SkillExecution.objects.filter(pk=execution_id).update(
                status=ExecutionStatus.COMPLETED
            )
        mock_task.side_effect = synchronous_completion

        response = self.client.post(self._url(), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        mock_task.assert_called_once_with(self.execution.id)
        mock_task.delay.assert_not_called()

    def test_resuming_a_running_execution_is_rejected(self):
        self.execution.status = ExecutionStatus.RUNNING
        self.execution.save(update_fields=["status"])

        response = self.client.post(self._url(), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_viewer_without_mutate_access_cannot_resume(self):
        ProjectShare.objects.create(
            project=self.project, user=self.other_user, role=ProjectShareRole.VIEWER
        )
        self.client.force_authenticate(self.other_user)

        response = self.client.post(self._url(), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_user_with_no_access_gets_not_found(self):
        self.client.force_authenticate(self.other_user)
        response = self.client.post(self._url(), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
