"""
Sólo superadmins ejecutan asistentes.

El resto ve las operaciones, sus informes y el chat, pero no lanza, reanuda,
repite ni avanza corridas: cada una cuesta dinero y cambia el informe.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.skill.access import user_can_run_assistants
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillType
from apps.project.models import Project

User = get_user_model()


class RunPermissionTestCase(APITestCase):
    def setUp(self):
        self.member = User.objects.create_user(email="m@example.com", password="x", username="m")
        self.admin = User.objects.create_user(email="a@example.com", password="x", username="a", role="admin")
        self.project = Project.objects.create(owner=self.member, name="Op")
        self.skill = Skill.objects.create(
            owner=self.member, name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"]
        )
        self.project.enabled_skills.add(self.skill)
        self.execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.member, project=self.project, status=ExecutionStatus.FAILED
        )

    def test_who_can_run(self):
        superuser = User.objects.create_superuser(email="s@example.com", password="x", username="s")
        self.assertTrue(user_can_run_assistants(superuser))
        self.assertTrue(user_can_run_assistants(self.admin))
        self.assertFalse(user_can_run_assistants(self.member))

    def test_member_cannot_run_nor_continue_a_run(self):
        """Ni siquiera sobre su propia operación o su propia corrida."""
        self.client.force_authenticate(self.member)
        pk = self.execution.pk
        llamadas = [
            reverse("skill-run", kwargs={"slug": self.skill.slug}),
            reverse("skill-execution-resume", kwargs={"pk": pk}),
            reverse("skill-execution-rerun", kwargs={"pk": pk}),
            reverse("skill-execution-approve", kwargs={"pk": pk}),
            reverse("skill-execution-regenerate-step-action", kwargs={"pk": pk}),
        ]
        with patch("apps.skill.dispatch.run_skill_task.delay") as delay:
            for url in llamadas:
                with self.subTest(url=url):
                    response = self.client.post(
                        url, {"context_type": "project", "context_slug": self.project.slug}, format="json"
                    )
                    self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        delay.assert_not_called()
        self.assertEqual(SkillExecution.objects.count(), 1)

    def test_member_can_still_read_executions(self):
        self.client.force_authenticate(self.member)
        response = self.client.get(reverse("skill-execution-detail", kwargs={"pk": self.execution.pk}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
