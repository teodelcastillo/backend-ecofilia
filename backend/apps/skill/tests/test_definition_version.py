"""
Resolución de versión contra la base real: crear, reutilizar, no duplicar.

Esto es lo único de ``definition.py`` que necesita base de datos — todo lo
demás (serializar, hashear, diffear) trabaja por ``getattr`` y ya está cubierto
en ``test_definition.py`` sin tocarla.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.skill.definition import resolve_definition_version
from apps.skill.models import Skill, SkillDefinitionVersion, SkillStep, SkillType

User = get_user_model()


class ResolveDefinitionVersionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="autor@example.com", password="secret123", username="autor",
        )
        self.skill = Skill.objects.create(
            owner=self.user,
            name="IET DATBC",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
        )
        self.step = SkillStep.objects.create(
            skill=self.skill, title="Marco normativo", instructions="Describí el marco.",
        )

    def test_first_call_creates_version_one(self):
        version = resolve_definition_version(self.skill)
        self.assertEqual(version.version_number, 1)
        self.assertEqual(SkillDefinitionVersion.objects.filter(skill=self.skill).count(), 1)

    def test_calling_again_without_changes_reuses_the_same_version(self):
        first = resolve_definition_version(self.skill)
        second = resolve_definition_version(self.skill)

        self.assertEqual(first.id, second.id)
        self.assertEqual(SkillDefinitionVersion.objects.filter(skill=self.skill).count(), 1)

    def test_editing_a_step_creates_a_new_version(self):
        first = resolve_definition_version(self.skill)

        self.step.instructions = "Describí el marco normativo vigente."
        self.step.save(update_fields=["instructions"])

        second = resolve_definition_version(self.skill)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(second.version_number, 2)

    def test_reverting_an_edit_returns_to_the_original_version_not_a_third(self):
        """La identidad es la huella, no el número: dos definiciones idénticas
        son la misma versión aunque se haya editado y revertido en el medio."""
        original = resolve_definition_version(self.skill)

        self.step.instructions = "Instrucción editada."
        self.step.save(update_fields=["instructions"])
        resolve_definition_version(self.skill)

        self.step.instructions = "Describí el marco."
        self.step.save(update_fields=["instructions"])
        reverted = resolve_definition_version(self.skill)

        self.assertEqual(original.id, reverted.id)
        self.assertEqual(SkillDefinitionVersion.objects.filter(skill=self.skill).count(), 2)

    def test_two_different_skills_do_not_collide_on_version_number(self):
        other_skill = Skill.objects.create(
            owner=self.user, name="Otra skill", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
        )
        v1 = resolve_definition_version(self.skill)
        v2 = resolve_definition_version(other_skill)

        self.assertEqual(v1.version_number, 1)
        self.assertEqual(v2.version_number, 1)
        self.assertNotEqual(v1.fingerprint, v2.fingerprint)
