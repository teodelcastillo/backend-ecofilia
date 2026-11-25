"""Duplicar un asistente: qué se copia, de qué alcance queda y quién puede."""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.skill.models import Skill, SkillParameter, SkillScope, SkillStep, SkillType
from apps.user.models import AuditEvent, Organization
from apps.user.tests.fixtures import make_superadmin

User = get_user_model()


def _copilot(slug, **extra):
    skill = Skill.objects.create(
        name=f"Copilot {slug}", slug=slug, skill_type=SkillType.COPILOT,
        allowed_contexts=["project"], is_template=True, is_default_enabled=True, **extra,
    )
    SkillStep.objects.create(skill=skill, position=1, title="Uno", instructions="Primero")
    SkillStep.objects.create(skill=skill, position=2, title="Dos", instructions="Segundo")
    SkillParameter.objects.create(skill=skill, key="pais", label="País", position=1)
    return skill


class DuplicateSkillTestCase(APITestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="A", slug="inst-a")
        self.restricted = Organization.objects.create(name="R", slug="inst-r", restricted=True)
        self.superadmin = make_superadmin()
        self.member = User.objects.create_user(email="m@a.org", password="x", organization=self.org)
        self.restricted_member = User.objects.create_user(
            email="m@r.org", password="x", organization=self.restricted
        )
        self.public = _copilot("publico", scope=SkillScope.PUBLIC)
        self.of_org = _copilot("de-inst", scope=SkillScope.ORGANIZATION, organization=self.org)
        self.restricted.enabled_skills.add(self.public)

    def _duplicate(self, user, slug, payload=None):
        self.client.force_authenticate(user)
        return self.client.post(f"/api/skills/{slug}/duplicate/", payload or {}, format="json")

    def test_copies_definition_steps_and_parameters(self):
        response = self._duplicate(self.member, "publico")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        copy = Skill.objects.get(slug=response.data["slug"])
        self.assertNotEqual(copy.pk, self.public.pk)
        self.assertEqual(copy.name, "Copilot publico (copia)")
        self.assertEqual(copy.skill_type, SkillType.COPILOT)
        self.assertEqual((copy.scope, copy.owner), (SkillScope.USER, self.member))
        self.assertFalse(copy.is_template)
        self.assertFalse(copy.is_default_enabled)
        self.assertEqual(
            list(copy.steps.order_by("position").values_list("title", "instructions")),
            [("Uno", "Primero"), ("Dos", "Segundo")],
        )
        self.assertEqual(list(copy.parameters.values_list("key", flat=True)), ["pais"])
        # El original no se toca.
        self.assertEqual(self.public.steps.count(), 2)
        self.assertEqual(self.public.parameters.count(), 1)

    def test_custom_name(self):
        response = self._duplicate(self.member, "publico", {"name": "  Mi variante  "})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["name"], "Mi variante")
        self.assertEqual(response.data["slug"], "mi-variante")

    def test_the_copy_is_editable_by_its_owner(self):
        slug = self._duplicate(self.member, "publico").data["slug"]
        copy = Skill.objects.get(slug=slug)
        self.assertTrue(copy.can_edit(self.member))
        self.assertFalse(self.public.can_edit(self.member))

    def test_superadmin_keeps_scope_and_institution_and_it_is_audited(self):
        response = self._duplicate(self.superadmin, "de-inst")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        copy = Skill.objects.get(slug=response.data["slug"])
        self.assertEqual((copy.scope, copy.organization), (SkillScope.ORGANIZATION, self.org))
        self.assertFalse(copy.enabled_for_organizations.exists())
        event = AuditEvent.objects.get(action="skill.duplicated")
        self.assertEqual(event.data["from"], "de-inst")

    def test_a_member_cannot_take_an_institution_skill_out(self):
        before = Skill.objects.count()
        response = self._duplicate(self.member, "de-inst")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Skill.objects.count(), before)

    def test_restricted_institutions_cannot_duplicate(self):
        response = self._duplicate(self.restricted_member, "publico")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_what_you_cannot_see_is_404(self):
        platform = _copilot("plataforma", scope=SkillScope.PLATFORM)
        self.assertEqual(self._duplicate(self.member, platform.slug).status_code, status.HTTP_404_NOT_FOUND)
        other = User.objects.create_user(email="o@x.org", password="x")
        self.assertEqual(self._duplicate(other, "de-inst").status_code, status.HTTP_404_NOT_FOUND)
