"""
Alta de instituciones (fase 5). Contrato en ``backend/docs/contrato-instituciones.md``.
"""
from django.contrib.auth import get_user_model
from django.core import mail
from rest_framework import status
from rest_framework.test import APITestCase

from apps.authentication.services.tokens import TokenService
from apps.skill.models import Skill, SkillParameter, SkillScope, SkillStep, SkillType
from apps.user.models import Organization, PlatformConfig
from apps.user.tests.fixtures import caf_config

User = get_user_model()
URL = "/api/admin/organizations/"


def _template(launches=(), report=None):
    return {
        "version": 1,
        "branding": {"display_name": "Nueva institución", "logo_url": ""},
        "stages": [
            {"slug": "inicio", "label": "Inicio", "principal_tag": None, "launches": list(launches)},
            {"slug": "cierre", "label": "Cierre", "principal_tag": None, "launches": []},
        ],
        "workflows": {"report": report, "guided": None, "reading": None},
        "evidence_tag_groups": [],
        "evaluation": {"framework": "", "criteria": []},
    }


class _OnboardingBase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")
        self.client.force_authenticate(self.superadmin)
        self.workflow = Skill.objects.create(
            name="Informe Ecofilia", slug="informe-ecofilia", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"], scope=SkillScope.PLATFORM, members_can_run=True,
        )
        SkillStep.objects.create(skill=self.workflow, title="1. Contexto", instructions="Describí", position=1)
        SkillStep.objects.create(skill=self.workflow, title="2. Cierre", instructions="Concluí", position=2)
        SkillParameter.objects.create(skill=self.workflow, key="foco", label="Foco")
        self.caf_skill = Skill.objects.create(
            name="De CAF", slug="caf-propia", skill_type=SkillType.COPILOT, allowed_contexts=["project"],
            scope=SkillScope.ORGANIZATION, organization=self.caf,
        )

    def _create(self, **payload):
        body = {"name": "Banco del Sur", "slug": "banco-del-sur", **payload}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(URL, body, format="json")


class CreateInstitutionTestCase(_OnboardingBase):
    def test_minimal(self):
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        org = Organization.objects.get(slug="banco-del-sur")
        self.assertEqual((org.name, org.restricted, org.enabled_features), ("Banco del Sur", False, []))
        self.assertEqual(response.data["cloned_skills"], [])
        self.assertEqual(response.data["members"], [])
        # Sin plantilla guardada, la plantilla es la base: sin etapas, con su nombre.
        self.assertEqual(org.config["stages"], [])
        self.assertEqual(org.config["branding"]["display_name"], "Banco del Sur")

    def test_from_the_template_with_cloned_skills_pointing_to_the_copies(self):
        PlatformConfig.objects.create(
            key="template", config=_template(launches=["informe-ecofilia"], report="informe-ecofilia")
        )
        response = self._create(
            restricted=True, enabled_features=["projects", "chat"], clone_skills=["informe-ecofilia"]
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["cloned_skills"], [{"from": "informe-ecofilia", "slug": "banco-del-sur-informe-ecofilia"}]
        )
        org = Organization.objects.get(slug="banco-del-sur")
        clone = Skill.objects.get(slug="banco-del-sur-informe-ecofilia")
        self.assertEqual((clone.scope, clone.organization, clone.owner_id), ("organization", org, None))
        self.assertTrue(clone.members_can_run)
        self.assertEqual(list(clone.steps.values_list("title", flat=True)), ["1. Contexto", "2. Cierre"])
        self.assertEqual(list(clone.parameters.values_list("key", flat=True)), ["foco"])
        # El original no cambia.
        self.assertEqual(self.workflow.steps.count(), 2)
        self.workflow.refresh_from_db()
        self.assertEqual(self.workflow.scope, "platform")

        self.assertEqual(org.config["stages"][0]["launches"], ["banco-del-sur-informe-ecofilia"])
        self.assertEqual(org.config["workflows"]["report"], "banco-del-sur-informe-ecofilia")
        self.assertEqual(org.config["branding"]["display_name"], "Banco del Sur")
        self.assertEqual(response.data["enabled_skill_slugs"], ["banco-del-sur-informe-ecofilia"])
        self.assertEqual(response.data["default_skill_slugs"], ["banco-del-sur-informe-ecofilia"])

    def test_without_template(self):
        PlatformConfig.objects.create(key="template", config=_template())
        self._create(from_template=False)
        self.assertEqual(Organization.objects.get(slug="banco-del-sur").config, {})

    def test_another_institution_skill_cannot_be_cloned(self):
        response = self._create(clone_skills=["caf-propia"])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("clone_skills", response.data)
        self.assertFalse(Organization.objects.filter(slug="banco-del-sur").exists())

    def test_an_invalid_resulting_config_rolls_everything_back(self):
        # La plantilla lanza una skill de CAF: no es pública ni de la institución nueva.
        PlatformConfig.objects.create(key="template", config=_template(launches=["caf-propia"]))
        response = self._create(clone_skills=["informe-ecofilia"], members=[{"email": "ana@sur.org"}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("config", response.data)
        self.assertFalse(Organization.objects.filter(slug="banco-del-sur").exists())
        self.assertFalse(Skill.objects.filter(slug__startswith="banco-del-sur-").exists())
        self.assertFalse(User.objects.filter(email="ana@sur.org").exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_slug_and_name_rules(self):
        cases = [
            ({"slug": "Banco Sur"}, "slug"),
            ({"slug": "admin"}, "slug"),
            ({"slug": "app"}, "slug"),
            ({"slug": "me"}, "slug"),
            ({"slug": "caf"}, "slug"),
            ({"name": "CAF"}, "name"),
            ({"name": ""}, "name"),
            ({"enabled_features": ["nada"]}, "enabled_features"),
            ({"clone_skills": ["no-existe"]}, "clone_skills"),
            ({"color": "verde"}, "detail"),
        ]
        for payload, field in cases:
            response = self._create(**payload)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)
            self.assertIn(field, response.data, payload)
        self.assertEqual(Organization.objects.count(), 1)

    def test_only_superadmin(self):
        member = User.objects.create_user(email="m@caf.org", password="x", organization=self.caf, role="admin")
        self.client.force_authenticate(member)
        self.assertEqual(self._create().status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.post("/api/admin/organizations/caf/members/", {"email": "x@y.org"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )


class MembersTestCase(_OnboardingBase):
    def test_new_and_existing_members(self):
        existing = User.objects.create_user(email="juan@gmail.com", password="x", email_verified=True)
        response = self._create(
            members=[{"email": "Ana@Sur.org", "role": "admin"}, {"email": "juan@gmail.com"}]
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        by_email = {m["email"]: m for m in response.data["members"]}
        self.assertEqual(
            (by_email["ana@sur.org"]["created"], by_email["ana@sur.org"]["invited"]), (True, True)
        )
        self.assertEqual((by_email["juan@gmail.com"]["created"], by_email["juan@gmail.com"]["invited"]), (False, False))
        org = Organization.objects.get(slug="banco-del-sur")
        ana = User.objects.get(email="ana@sur.org")
        self.assertEqual((ana.organization, ana.role, ana.approved, ana.email_verified), (org, "admin", True, False))
        self.assertFalse(ana.has_usable_password())
        existing.refresh_from_db()
        self.assertEqual((existing.organization, existing.role), (org, "member"))

        self.assertEqual([m.to for m in mail.outbox], [["ana@sur.org"]])
        self.assertIn("Banco del Sur", mail.outbox[0].subject)
        self.assertIn("/auth/reset-password?uid=", mail.outbox[0].body)
        self.assertIn("invite=1", mail.outbox[0].body)

    def test_members_that_cannot_join(self):
        User.objects.create_user(email="ya@caf.org", password="x", organization=self.caf)
        User.objects.create_user(email="staff@ecofilia.site", password="x", is_staff=True)
        for email in ("ya@caf.org", "staff@ecofilia.site", "root@ecofilia.site", "no-es-un-email"):
            response = self._create(members=[{"email": email}])
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, email)
            self.assertIn("members", response.data, email)
        self.assertFalse(Organization.objects.filter(slug="banco-del-sur").exists())

    def test_add_a_member_to_an_existing_institution_and_reinvite(self):
        url = "/api/admin/organizations/caf/members/"
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"email": "nueva@caf.org"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual((response.data["created"], response.data["invited"]), (True, True))
        # Volver a agregarla sin que haya entrado le reenvía la invitación.
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"email": "nueva@caf.org", "role": "manager"}, format="json")
        self.assertEqual((response.data["created"], response.data["invited"]), (False, True))
        self.assertEqual(User.objects.get(email="nueva@caf.org").role, "manager")
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            self.client.post("/api/admin/organizations/no-existe/members/", {"email": "a@b.org"}, format="json")
            .status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_choosing_the_password_verifies_the_email_and_lets_in(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/api/admin/organizations/caf/members/", {"email": "nueva@caf.org"}, format="json")
        user = User.objects.get(email="nueva@caf.org")
        self.client.force_authenticate(None)
        login = {"email": "nueva@caf.org", "password": "Una-clave-larga-2026"}
        self.assertEqual(self.client.post("/api/auth/login/", login, format="json").status_code, 401)
        response = self.client.post(
            "/api/auth/password/reset/confirm/",
            {
                "uid": TokenService.make_uid(user),
                "token": TokenService.generate_token(user),
                "new_password": "Una-clave-larga-2026",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        user.refresh_from_db()
        self.assertTrue(user.email_verified)
        response = self.client.post("/api/auth/login/", login, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["user"]["home_path"], "/caf")
