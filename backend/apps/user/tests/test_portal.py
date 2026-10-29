"""
El portal genérico ``/{institución}`` (fase 3). Contrato en
``backend/docs/contrato-instituciones.md``.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.document.models import Document
from apps.project.models import Project
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillScope, SkillType
from apps.user.models import Organization
from apps.user.tests.fixtures import caf_config

User = get_user_model()


class PortalTestCase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", restricted=True, config=caf_config())
        self.other = Organization.objects.create(name="Otra institución", slug="otra")
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")
        self.caf_member = User.objects.create_user(email="m@caf.org", password="x", organization=self.caf)
        self.other_member = User.objects.create_user(email="m@otra.org", password="x", organization=self.other)
        self.public = User.objects.create_user(email="persona@gmail.com", password="x")

    def _as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_home_path(self):
        expected = {
            self.superadmin: "/admin",
            self.caf_member: "/caf",
            self.other_member: "/otra",
            self.public: "/app",
        }
        for user, path in expected.items():
            self._as(user)
            self.assertEqual(self.client.get("/api/auth/me/").data["home_path"], path, user.email)

    def test_login_carries_home_path(self):
        self.caf_member.email_verified = True
        self.caf_member.save(update_fields=["email_verified"])
        response = self.client.post("/api/auth/login/", {"email": "m@caf.org", "password": "x"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["user"]["home_path"], "/caf")

    def test_portals_each_one_can_open(self):
        expected = {
            self.superadmin: ["caf", "otra"],
            self.caf_member: ["caf"],
            self.other_member: ["otra"],
            self.public: [],
        }
        for user, slugs in expected.items():
            self._as(user)
            data = self.client.get("/api/organizations/").data
            self.assertEqual([p["slug"] for p in data], slugs, user.email)
        self._as(self.caf_member)
        caf = self.client.get("/api/organizations/").data[0]
        self.assertEqual((caf["display_name"], caf["logo_url"]), ("CAF", "/images/caf-logo.png"))
        self._as(self.other_member)
        self.assertEqual(self.client.get("/api/organizations/").data[0]["display_name"], "Otra institución")

    def test_historical_report_requires_the_institution(self):
        Document.objects.create(owner=self.superadmin, name="IET", slug="iet-viejo")
        self.client.force_authenticate(self.superadmin)
        payload = {"document_slug": "iet-viejo", "name": "Op vieja", "pais": "Perú"}
        response = self.client.post("/api/projects/historical-report/", payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Project.objects.filter(name="Op vieja").exists())
        with patch("apps.skill.dispatch.run_skill_task.delay"):
            response = self.client.post(
                "/api/projects/historical-report/", {**payload, "organization": "otra"}, format="json"
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(Project.objects.get(name="Op vieja").organization, self.other)

    def test_analysis_running_follows_the_institution_report(self):
        report = Skill.objects.create(
            name="Informe de otra", slug="informe-otra", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"], scope=SkillScope.ORGANIZATION, organization=self.other,
        )
        config = caf_config()
        config["stages"] = [{"slug": "a", "label": "A", "principal_tag": None, "launches": ["informe-otra"]}]
        config["workflows"] = {"report": "informe-otra", "guided": None, "reading": None}
        config["evaluation"] = {"framework": "", "criteria": []}
        self.other.config = config
        self.other.save()
        op = Project.objects.create(owner=self.other_member, name="Op otra")
        SkillExecution.objects.create(skill=report, owner=self.other_member, project=op, status=ExecutionStatus.RUNNING)
        self._as(self.other_member)
        rows = self.client.get("/api/projects/?organization=otra").data
        self.assertTrue(next(p for p in rows if p["slug"] == op.slug)["analysis_running"])
