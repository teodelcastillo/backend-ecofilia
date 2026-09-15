from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from apps.project.models import Project
from apps.skill.models import Skill, SkillType
from apps.user.models import Organization

User = get_user_model()


class BackfillDefaultSkillsCommandTestCase(TestCase):
    """
    El caso real: alguien sin organización asignada (típicamente staff) crea
    una operación para un cliente con `default_project_skills` configurado.
    `_default_skills_for` mira `project.owner.organization`, que en ese caso
    es None, así que la operación nace sin ningún agente — y el portal
    restringido no le da al usuario ninguna forma de asignarle uno después.
    """

    def setUp(self):
        self.org = Organization.objects.create(name="CAF", slug="caf", restricted=True)
        self.iet = Skill.objects.create(
            name="IET", skill_type=SkillType.COPILOT, owner=None,
            allowed_contexts=["project"],
        )
        self.org.default_project_skills.add(self.iet)

        self.staff = User.objects.create_user(
            email="staff@ecofilia.com", password="secret123", username="staff",
        )  # sin organización

    def test_owner_without_organization_is_left_alone(self):
        """
        Sin organización en el owner no hay de dónde sacar un default — el
        comando no tiene nada que inventar. Es justamente el estado en el que
        una operación queda huérfana en primer lugar; arreglarlo es cosa del
        formulario de creación, no de este comando.
        """
        orphan = Project.objects.create(owner=self.staff, name="Operación sin agente")

        call_command("backfill_default_skills")

        self.assertEqual(orphan.enabled_skills.count(), 0)

    def test_orphan_owned_by_org_member_gets_fixed(self):
        self.staff.organization = self.org
        self.staff.save(update_fields=["organization"])
        orphan = Project.objects.create(owner=self.staff, name="Operación sin agente")

        out = StringIO()
        call_command("backfill_default_skills", stdout=out)

        orphan.refresh_from_db()
        self.assertEqual(
            list(orphan.enabled_skills.values_list("slug", flat=True)), [self.iet.slug]
        )
        self.assertIn("1 operación", out.getvalue())

    def test_dry_run_does_not_write(self):
        self.staff.organization = self.org
        self.staff.save(update_fields=["organization"])
        orphan = Project.objects.create(owner=self.staff, name="Operación sin agente")

        out = StringIO()
        call_command("backfill_default_skills", "--dry-run", stdout=out)

        self.assertEqual(orphan.enabled_skills.count(), 0)
        self.assertIn("Dry-run", out.getvalue())

    def test_does_not_touch_a_project_with_skills_already(self):
        """Una lista de agentes no vacía refleja una elección deliberada."""
        self.staff.organization = self.org
        self.staff.save(update_fields=["organization"])
        other_skill = Skill.objects.create(
            name="Otro", skill_type=SkillType.QUICK, owner=None,
            allowed_contexts=["project"],
        )
        project = Project.objects.create(owner=self.staff, name="Con agente propio")
        project.enabled_skills.add(other_skill)

        call_command("backfill_default_skills")

        self.assertEqual(
            list(project.enabled_skills.values_list("slug", flat=True)), [other_skill.slug]
        )

    def test_scoped_to_a_single_organization(self):
        other_org = Organization.objects.create(name="Otra", slug="otra")
        other_skill = Skill.objects.create(
            name="Agente de otra", skill_type=SkillType.QUICK, owner=None,
            allowed_contexts=["project"],
        )
        other_org.default_project_skills.add(other_skill)
        other_user = User.objects.create_user(
            email="otra@example.com", password="secret123", username="otra",
            organization=other_org,
        )
        other_orphan = Project.objects.create(owner=other_user, name="Operación de otra org")

        self.staff.organization = self.org
        self.staff.save(update_fields=["organization"])
        caf_orphan = Project.objects.create(owner=self.staff, name="Operación CAF")

        call_command("backfill_default_skills", "--org-slug", "caf")

        caf_orphan.refresh_from_db()
        other_orphan.refresh_from_db()
        self.assertEqual(caf_orphan.enabled_skills.count(), 1)
        self.assertEqual(other_orphan.enabled_skills.count(), 0)
