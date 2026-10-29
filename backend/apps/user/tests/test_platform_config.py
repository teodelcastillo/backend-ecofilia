"""
Configuración pública (``/app``) y plantilla (portal base), fase 4. Contrato en
``backend/docs/contrato-instituciones.md``.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.project.models import Project
from apps.project.services.stages import stage_order, stage_workflow_slugs
from apps.skill.models import Skill, SkillScope, SkillType
from apps.user import institution_config
from apps.user.models import Organization, PlatformConfig
from apps.user.tests.fixtures import caf_config

User = get_user_model()


def _config(stages, launches=()):
    return {
        "version": 1,
        "branding": {"display_name": "Ecofilia", "logo_url": ""},
        "stages": [
            {"slug": s, "label": s.capitalize(), "principal_tag": None,
             "launches": list(launches) if i == 0 else []}
            for i, s in enumerate(stages)
        ],
        "workflows": {"report": None, "guided": None, "reading": None},
        "evidence_tag_groups": [],
        "evaluation": {"framework": "", "criteria": []},
    }


class PlatformConfigTestCase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")
        self.member = User.objects.create_user(email="m@caf.org", password="x", organization=self.caf)
        self.public = User.objects.create_user(email="persona@gmail.com", password="x")
        self.open_skill = Skill.objects.create(
            name="Abierta", slug="abierta", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"], scope=SkillScope.PUBLIC,
        )

    def test_without_saving_both_are_the_base(self):
        self.client.force_authenticate(self.public)
        self.assertEqual(
            self.client.get("/api/platform/config/public/").data["config"], institution_config.BASE_CONFIG
        )
        self.client.force_authenticate(self.superadmin)
        self.assertEqual(
            self.client.get("/api/platform/config/template/").data["config"], institution_config.BASE_CONFIG
        )

    def test_who_reads_and_writes(self):
        for user in (self.member, self.public):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get("/api/platform/config/public/").status_code, 200)
            self.assertEqual(self.client.get("/api/platform/config/template/").status_code, 403)
            for key in ("public", "template"):
                response = self.client.put(f"/api/platform/config/{key}/", {"config": _config(["a"])}, format="json")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, (user.email, key))

    def test_the_public_config_rules_the_app_and_orgless_operations(self):
        self.client.force_authenticate(self.superadmin)
        response = self.client.put(
            "/api/platform/config/public/",
            {"config": _config(["borrador", "listo"], launches=["abierta"])},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.client.force_authenticate(self.public)
        mine = self.client.get("/api/organizations/me/config/").data
        self.assertIsNone(mine["organization"])
        self.assertEqual([s["slug"] for s in mine["config"]["stages"]], ["borrador", "listo"])

        project = Project.objects.create(owner=self.public, name="Op pública")
        self.assertEqual(stage_order(project), ["borrador", "listo"])
        self.assertEqual(stage_workflow_slugs(project, "borrador"), ["abierta"])
        # Una institución sin configuración propia no hereda la pública.
        unconfigured = Organization.objects.create(name="Banco X", slug="banco-x")
        self.assertEqual(institution_config.effective_config(unconfigured)["stages"], [])
        # CAF sigue con la suya.
        self.assertEqual(institution_config.effective_config(self.caf)["stages"][1]["slug"], "ido_cci")

    def test_they_are_validated_as_without_institution(self):
        caf_skill = Skill.objects.create(
            name="De CAF", slug="de-caf", skill_type=SkillType.COPILOT, allowed_contexts=["project"],
            scope=SkillScope.ORGANIZATION, organization=self.caf,
        )
        self.client.force_authenticate(self.superadmin)
        for key in ("public", "template"):
            response = self.client.put(
                f"/api/platform/config/{key}/", {"config": _config(["a"], launches=[caf_skill.slug])}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, key)
            self.assertTrue(any("de-caf" in e for e in response.data["config"]), response.data)
        self.assertFalse(PlatformConfig.objects.exists())

    def test_the_template_is_separate_from_the_public(self):
        self.client.force_authenticate(self.superadmin)
        self.client.put("/api/platform/config/template/", {"config": _config(["inicio", "cierre"])}, format="json")
        self.assertEqual(
            [s["slug"] for s in institution_config.platform_config("template")["stages"]], ["inicio", "cierre"]
        )
        self.assertEqual(institution_config.platform_config("public")["stages"], [])
