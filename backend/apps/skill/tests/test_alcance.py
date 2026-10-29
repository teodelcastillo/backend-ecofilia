"""
El alcance de los asistentes (fase 0): escribirlo, versionarlo, migrarlo y listarlo.

La visibilidad que resulta del alcance se recorre en
``apps.project.tests.test_aislamiento``.
"""
import importlib
from io import StringIO

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import EvidenceTag
from apps.project.models import Project
from apps.skill.definition import resolve_definition_version
from apps.skill.models import (
    Skill,
    SkillDefinitionVersion,
    SkillScope,
    SkillStep,
    SkillType,
)
from apps.user.models import Organization

User = get_user_model()


def _quick(slug, **extra):
    return Skill.objects.create(
        name=slug, slug=slug, skill_type=SkillType.QUICK,
        prompt_template="{{context}}", allowed_contexts=["document"], **extra,
    )


class SkillScopeWriteTestCase(APITestCase):
    def setUp(self):
        self.org_a = Organization.objects.create(name="A", slug="inst-a")
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")
        self.member = User.objects.create_user(email="m@a.org", password="x", organization=self.org_a)
        self.skill = _quick("asistente", scope=SkillScope.PUBLIC)

    def _patch(self, user, payload, slug="asistente"):
        self.client.force_authenticate(user)
        return self.client.patch(f"/api/skills/{slug}/", payload, format="json")

    def test_superadmin_moves_a_skill_to_an_institution(self):
        response = self._patch(self.superadmin, {"scope": "organization", "organization_slug": "inst-a"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.data["scope"], response.data["organization_slug"]), ("organization", "inst-a"))
        self.skill.refresh_from_db()
        self.assertEqual((self.skill.scope, self.skill.organization), (SkillScope.ORGANIZATION, self.org_a))

    def test_organization_scope_requires_the_institution(self):
        response = self._patch(self.superadmin, {"scope": "organization"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("organization_slug", response.data)
        response = self._patch(self.superadmin, {"scope": "organization", "organization_slug": "no-existe"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_other_scopes_leave_the_institution_null(self):
        self.skill.scope = SkillScope.ORGANIZATION
        self.skill.organization = self.org_a
        self.skill.save()
        response = self._patch(self.superadmin, {"scope": "platform", "organization_slug": "inst-a"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.data["organization_slug"])
        self.skill.refresh_from_db()
        self.assertEqual((self.skill.scope, self.skill.organization_id), (SkillScope.PLATFORM, None))

    def test_only_superadmin_changes_the_scope(self):
        response = self._patch(self.member, {"scope": "organization", "organization_slug": "inst-a"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        own = _quick("mia", owner=self.member)
        response = self._patch(self.member, {"scope": "public"}, slug=own.slug)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        own.refresh_from_db()
        self.assertEqual(own.scope, SkillScope.USER)

    def test_a_scope_change_does_not_touch_the_definition(self):
        workflow = Skill.objects.create(
            name="wf", slug="wf", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"], comparative_mode_enabled=False,
        )
        SkillStep.objects.create(skill=workflow, title="1", instructions="x", position=1)
        response = self._patch(self.superadmin, {"scope": "public"}, slug="wf")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        workflow.refresh_from_db()
        self.assertEqual(workflow.scope, SkillScope.PUBLIC)
        self.assertFalse(workflow.comparative_mode_enabled)
        self.assertEqual(workflow.steps.count(), 1)

    def test_a_skill_with_an_owner_and_no_scope_is_the_owners(self):
        self.assertEqual(_quick("con-duenio", owner=self.member).scope, SkillScope.USER)
        self.assertEqual(_quick("sin-duenio").scope, SkillScope.PLATFORM)


class DefinitionVersionInformedByTestCase(APITestCase):
    def setUp(self):
        self.org_a = Organization.objects.create(name="A", slug="inst-a")
        self.org_b = Organization.objects.create(name="B", slug="inst-b")
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")
        self.skill = _quick("de-a", scope=SkillScope.ORGANIZATION, organization=self.org_a)

    def test_a_version_of_an_institution_skill_is_informed_by_it(self):
        version = resolve_definition_version(self.skill)
        self.assertEqual(version.informed_by_organization, self.org_a)

    def test_saving_a_version_informed_by_another_institution_fails(self):
        version = resolve_definition_version(self.skill)
        version.informed_by_organization = self.org_b
        with self.assertRaises(ValidationError):
            version.save()
        version.informed_by_organization = None
        with self.assertRaises(ValidationError):
            version.save()

    def test_the_api_answers_400(self):
        version = resolve_definition_version(self.skill)
        self.client.force_authenticate(self.superadmin)
        url = f"/api/skills/de-a/definition-versions/{version.version_number}/"
        response = self.client.patch(url, {"informed_by_organization_slug": "inst-b"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        version.refresh_from_db()
        self.assertEqual(version.informed_by_organization, self.org_a)
        response = self.client.patch(url, {"informed_by_organization_slug": "inst-a"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["informed_by_organization_slug"], "inst-a")
        listing = self.client.get("/api/skills/de-a/definition-versions/").data
        self.assertEqual(listing[0]["informed_by_organization_slug"], "inst-a")

    def test_other_scopes_take_any_institution(self):
        public = _quick("abierta", scope=SkillScope.PUBLIC)
        version = resolve_definition_version(public)
        self.assertIsNone(version.informed_by_organization)
        version.informed_by_organization = self.org_b
        version.save()


def _migration(app, name):
    return importlib.import_module(f"apps.{app}.migrations.{name}")


class ScopeDataMigrationTestCase(TestCase):
    """Las migraciones de datos, aplicadas sobre filas con la forma de producción."""

    def setUp(self):
        self.owner = User.objects.create_user(email="duenio@example.com", password="x")
        self.caf_iet = _quick("caf-iet-x")
        self.owned = _quick("propia-x", owner=self.owner)
        self.template = _quick("plantilla-x", scope=SkillScope.PUBLIC)
        SkillDefinitionVersion.objects.create(
            skill=self.caf_iet, version_number=1, fingerprint="f", definition={}
        )

    def _apply(self):
        _migration("skill", "0049_asignar_alcance_skills").asignar(django_apps, None)
        for skill in (self.caf_iet, self.owned, self.template):
            skill.refresh_from_db()

    def test_without_caf_everything_ownerless_is_platform_and_nothing_fails(self):
        self._apply()
        self.assertEqual((self.caf_iet.scope, self.caf_iet.organization_id), (SkillScope.PLATFORM, None))
        self.assertEqual(self.owned.scope, SkillScope.USER)
        self.assertEqual(self.template.scope, SkillScope.PLATFORM)

    def test_with_caf_the_caf_skills_are_caf_and_reversible(self):
        caf = Organization.objects.create(name="CAF", slug="caf", restricted=True)
        self._apply()
        self.assertEqual((self.caf_iet.scope, self.caf_iet.organization), (SkillScope.ORGANIZATION, caf))
        self.assertEqual(self.caf_iet.definition_versions.get().informed_by_organization, caf)
        self.assertEqual(self.owned.scope, SkillScope.USER)
        self.assertEqual(self.template.scope, SkillScope.PLATFORM)

        _migration("skill", "0049_asignar_alcance_skills").revertir(django_apps, None)
        self.caf_iet.refresh_from_db()
        self.assertEqual((self.caf_iet.scope, self.caf_iet.organization_id), (SkillScope.PLATFORM, None))
        self.assertIsNone(self.caf_iet.definition_versions.get().informed_by_organization_id)

    def test_caf_operation_tags_go_to_caf_and_country_tags_stay_global(self):
        tags = _migration("document", "0024_etiquetas_de_operacion_caf")
        for slug in ("ido", "ias", "ndc", "nap"):
            EvidenceTag.objects.update_or_create(slug=slug, organization=None, defaults={"name": slug})
        tags.asignar(django_apps, None)  # sin CAF: no toca nada
        self.assertFalse(EvidenceTag.objects.filter(organization__isnull=False).exists())

        caf = Organization.objects.create(name="CAF", slug="caf")
        tags.asignar(django_apps, None)
        self.assertEqual(EvidenceTag.objects.get(slug="ido").organization, caf)
        self.assertEqual(EvidenceTag.objects.get(slug="ias").organization, caf)
        self.assertIsNone(EvidenceTag.objects.get(slug="ndc").organization_id)
        self.assertIsNone(EvidenceTag.objects.get(slug="nap").organization_id)

        tags.revertir(django_apps, None)
        self.assertFalse(EvidenceTag.objects.filter(organization__isnull=False).exists())

    def test_a_caf_operation_still_finds_its_caf_tags(self):
        from apps.project.services.evidence_tags import project_evidence_tags

        caf = Organization.objects.create(name="CAF", slug="caf")
        EvidenceTag.objects.update_or_create(slug="ido", organization=None, defaults={"name": "IDO"})
        _migration("document", "0024_etiquetas_de_operacion_caf").asignar(django_apps, None)
        member = User.objects.create_user(email="m@caf.org", password="x", organization=caf)
        caf_op = Project.objects.create(owner=member, name="Op CAF")
        other_op = Project.objects.create(owner=self.owner, name="Op pública")
        self.assertTrue(project_evidence_tags(caf_op.pk).filter(slug="ido").exists())
        self.assertFalse(project_evidence_tags(other_op.pk).filter(slug="ido").exists())
        self.assertTrue(project_evidence_tags(other_op.pk).filter(slug="ndc").exists())


class ScopeCommandsTestCase(TestCase):
    def setUp(self):
        self.org_a = Organization.objects.create(name="A", slug="inst-a")
        self.member = User.objects.create_user(email="m@a.org", password="x", organization=self.org_a)
        self.public = User.objects.create_user(email="persona@gmail.com", password="x")
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")
        Project.objects.create(owner=self.member, name="Op 1")
        Project.objects.create(owner=self.member, name="Op 2")
        self.platform = _quick("de-plataforma")
        self.of_a = _quick("de-a", scope=SkillScope.ORGANIZATION, organization=self.org_a)

    def _call(self, *args):
        out = StringIO()
        call_command(*args, stdout=out)
        return out.getvalue()

    def test_listar_skills_alcance(self):
        out = self._call("listar_skills_alcance")
        self.assertIn("de-plataforma", out)
        self.assertRegex(out, r"de-a\s+organization\s+inst-a")

    def test_hacer_publicas_with_dry_run_writes_nothing(self):
        out = self._call("listar_skills_alcance", "--hacer-publicas", "de-plataforma,de-a", "--dry-run")
        self.assertIn("de-plataforma: platform → public", out)
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.scope, SkillScope.PLATFORM)

    def test_hacer_publicas(self):
        self._call("listar_skills_alcance", "--hacer-publicas", "de-plataforma,de-a")
        for skill in (self.platform, self.of_a):
            skill.refresh_from_db()
            self.assertEqual((skill.scope, skill.organization_id), (SkillScope.PUBLIC, None))

    def test_hacer_publicas_with_an_unknown_slug_changes_nothing(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self._call("listar_skills_alcance", "--hacer-publicas", "de-plataforma,no-existe")
        self.platform.refresh_from_db()
        self.assertEqual(self.platform.scope, SkillScope.PLATFORM)

    def test_listar_usuarios_por_nivel(self):
        out = self._call("listar_usuarios_por_nivel")
        superadmin, rest = out.split("== Institución: inst-a")
        institution, public = rest.split("== Público")
        self.assertIn("root@ecofilia.site", superadmin)
        self.assertIn("m@a.org", institution)
        self.assertRegex(institution, r"m@a.org\s+último login: nunca\s+operaciones: 2")
        self.assertIn("persona@gmail.com", public)
        self.assertNotIn("persona@gmail.com", institution)


class SembrarContratoLocalTestCase(TestCase):
    def test_seeds_every_level_and_is_idempotent(self):
        call_command("sembrar_contrato_local", stdout=StringIO())
        call_command("sembrar_contrato_local", stdout=StringIO())
        self.assertEqual(User.objects.filter(email__endswith="@local.test").count(), 5)
        self.assertEqual(Project.objects.filter(slug__startswith="local-").count(), 3)
        caf = Organization.objects.get(slug="caf")
        self.assertTrue(Skill.objects.filter(slug__startswith="caf-", organization=caf).exists())
        self.assertEqual(EvidenceTag.objects.get(slug="ido").organization, caf)
        self.assertTrue(User.objects.get(email="caf-miembro@local.test").check_password("ecofilia-local"))

    def test_refuses_production(self):
        from unittest.mock import patch

        from django.core.management.base import CommandError

        with patch.dict("os.environ", {"DJANGO_SETTINGS_MODULE": "main.settings.prod"}):
            with self.assertRaises(CommandError):
                call_command("sembrar_contrato_local", stdout=StringIO())
        self.assertFalse(User.objects.filter(email__endswith="@local.test").exists())
