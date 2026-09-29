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


class MembersCanRunTestCase(APITestCase):
    """Cada asistente decide si lo ejecutan también los que no son superadmin."""

    def setUp(self):
        from apps.user.models import Organization

        self.org = Organization.objects.create(name="CAF", slug="caf", restricted=True)
        self.member = User.objects.create_user(
            email="ejecutivo@example.com", password="x", username="ejecutivo", organization=self.org
        )
        self.admin = User.objects.create_user(
            email="admin@example.com", password="x", username="admin", role="admin"
        )
        self.project = Project.objects.create(owner=self.member, name="Operación")
        self.skill = Skill.objects.create(
            owner=None, name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"]
        )
        self.project.enabled_skills.add(self.skill)
        self.org.enabled_skills.add(self.skill)

    def _run(self):
        with patch("apps.skill.dispatch.run_skill_task.delay"):
            return self.client.post(
                reverse("skill-run", kwargs={"slug": self.skill.slug}),
                {"context_type": "project", "context_slug": self.project.slug},
                format="json",
            )

    def test_closed_by_default(self):
        self.client.force_authenticate(self.member)
        self.assertEqual(self._run().status_code, status.HTTP_403_FORBIDDEN)

    def test_member_runs_an_assistant_opened_to_members(self):
        self.skill.members_can_run = True
        self.skill.save(update_fields=["members_can_run"])
        self.client.force_authenticate(self.member)

        self.assertEqual(self._run().status_code, status.HTTP_202_ACCEPTED)

    def test_opening_it_does_not_bypass_the_organization(self):
        """Un restringido sigue sin ver lo que no está asignado a su organización."""
        self.skill.members_can_run = True
        self.skill.save(update_fields=["members_can_run"])
        self.org.enabled_skills.remove(self.skill)
        self.client.force_authenticate(self.member)

        self.assertEqual(self._run().status_code, status.HTTP_404_NOT_FOUND)

    def test_only_a_superadmin_opens_an_assistant(self):
        """Ni siquiera sobre un asistente propio: quién lo ejecuta no lo decide su autor."""
        outsider = User.objects.create_user(email="o@example.com", password="x", username="o")
        mine = Skill.objects.create(
            owner=outsider, name="Propio", skill_type=SkillType.COPILOT, allowed_contexts=["project"]
        )
        self.client.force_authenticate(outsider)

        response = self.client.patch(
            reverse("skill-detail", kwargs={"slug": mine.slug}),
            {"members_can_run": True},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        mine.refresh_from_db()
        self.assertFalse(mine.members_can_run)


class AssignToOrganizationTestCase(APITestCase):
    """La asignación a organizaciones se decide desde el editor del asistente."""

    def setUp(self):
        from apps.user.models import Organization

        self.org = Organization.objects.create(name="CAF", slug="caf", restricted=True)
        self.admin = User.objects.create_user(
            email="admin@example.com", password="x", username="admin", role="admin"
        )
        self.member = User.objects.create_user(
            email="m@example.com", password="x", username="m", organization=self.org
        )
        self.existing = Project.objects.create(owner=self.member, name="Operación vieja")
        self.skill = Skill.objects.create(
            owner=self.admin, name="Nuevo", skill_type=SkillType.COPILOT, allowed_contexts=["project"]
        )
        self.client.force_authenticate(self.admin)

    def _patch(self, **data):
        return self.client.patch(
            reverse("skill-detail", kwargs={"slug": self.skill.slug}), data, format="json"
        )

    def test_assigns_visibility_and_default_for_new_operations(self):
        response = self._patch(organization_slugs=["caf"], default_organization_slugs=["caf"])

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(self.org.enabled_skills.filter(pk=self.skill.pk).exists())
        self.assertTrue(self.org.default_project_skills.filter(pk=self.skill.pk).exists())
        # Las existentes no se tocan sin pedirlo.
        self.assertFalse(self.existing.enabled_skills.filter(pk=self.skill.pk).exists())

    def test_enabling_on_existing_operations_is_explicit(self):
        self._patch(organization_slugs=["caf"], enable_on_existing_operations=["caf"])

        self.assertTrue(self.existing.enabled_skills.filter(pk=self.skill.pk).exists())

    def test_unassigning_removes_it(self):
        self.org.enabled_skills.add(self.skill)

        self._patch(organization_slugs=[])

        self.assertFalse(self.org.enabled_skills.filter(pk=self.skill.pk).exists())

    def test_saving_without_the_fields_leaves_the_assignment_alone(self):
        self.org.enabled_skills.add(self.skill)

        self._patch(name="Renombrado")

        self.assertTrue(self.org.enabled_skills.filter(pk=self.skill.pk).exists())

    def test_organizations_listing_is_for_superadmins(self):
        listing = self.client.get(reverse("skill-organizations"))
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data[0]["slug"], "caf")
        self.assertEqual(listing.data[0]["operations_count"], 1)

        self.client.force_authenticate(self.member)
        self.assertEqual(
            self.client.get(reverse("skill-organizations")).status_code,
            status.HTTP_403_FORBIDDEN,
        )
