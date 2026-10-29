"""
Una persona de una organización sólo tiene acceso a su organización. Siempre.

Ver ``apps/user/access.py``. Los pedidos van con JWT de verdad, no con
``force_authenticate``: el alcance se aplica en la autenticación y
``force_authenticate`` la saltea.
"""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.document.models import Document
from apps.project.models import Project, ProjectShare
from apps.skill.models import Skill, SkillType
from apps.user.models import Organization

User = get_user_model()


class OrganizationScopeTestCase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", restricted=True)
        self.other = Organization.objects.create(name="Otro cliente", slug="otro")
        self.caf_member = User.objects.create_user(email="m@caf.com", password="x", organization=self.caf)
        self.caf_admin = User.objects.create_user(email="admin@caf.com", password="x", organization=self.caf, role="admin")
        self.other_member = User.objects.create_user(email="m@otro.com", password="x", organization=self.other)
        self.ecofilia_admin = User.objects.create_user(email="admin@ecofilia.site", password="x", role="admin")
        self.ecofilia_staff = User.objects.create_user(email="staff@ecofilia.site", password="x", is_staff=True)

        self.caf_op = Project.objects.create(owner=self.caf_member, name="Operación CAF")
        self.other_op = Project.objects.create(owner=self.other_member, name="Operación de otro cliente")

    def _as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _project_slugs(self):
        response = self.client.get("/api/projects/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {p["slug"] for p in response.data}

    # ── staff / superusuario mal cargados en un miembro ──────────────────

    def test_org_member_flagged_staff_still_sees_only_their_organization(self):
        User.objects.filter(pk=self.caf_admin.pk).update(is_staff=True, is_superuser=True)
        self._as(self.caf_admin)
        self.assertNotIn(self.other_op.slug, self._project_slugs())
        me = self.client.get("/api/auth/me/").data
        self.assertFalse(me["is_superuser"])

    def test_scope_is_not_persisted_when_the_user_saves(self):
        User.objects.filter(pk=self.caf_admin.pk).update(is_staff=True)
        self._as(self.caf_admin)
        response = self.client.patch("/api/auth/me/", {"first_name": "Ana"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.caf_admin.refresh_from_db()
        self.assertEqual(self.caf_admin.first_name, "Ana")
        self.assertTrue(self.caf_admin.is_staff, "el alcance de la API no debe tocar la base")

    def test_ecofilia_staff_keeps_global_access(self):
        self._as(self.ecofilia_staff)
        self.assertTrue({self.caf_op.slug, self.other_op.slug} <= self._project_slugs())

    def test_admin_refuses_staff_with_organization(self):
        self.caf_member.is_staff = True
        with self.assertRaises(ValidationError):
            self.caf_member.full_clean()
        self.caf_member.is_staff = False
        self.caf_member.is_superuser = True
        with self.assertRaises(ValidationError):
            self.caf_member.full_clean()
        self.ecofilia_staff.full_clean()  # sin organización: válido

    # ── lo que es de toda la plataforma ──────────────────────────────────

    def test_org_admin_cannot_publish_to_the_public_library(self):
        own = Document.objects.create(owner=self.caf_admin, name="Propio", slug="propio-caf")
        foreign = Document.objects.create(owner=self.other_member, name="Ajeno", slug="ajeno")
        self._as(self.caf_admin)
        response = self.client.post(
            "/api/document/bulk-public/", {"slugs": [own.slug, foreign.slug], "is_public": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.patch(f"/api/document/{own.slug}/", {"is_public": True}, format="json")
        own.refresh_from_db()
        foreign.refresh_from_db()
        self.assertFalse(own.is_public)
        self.assertFalse(foreign.is_public)

    def test_ecofilia_admin_still_publishes(self):
        doc = Document.objects.create(owner=self.caf_member, name="Curado", slug="curado")
        self._as(self.ecofilia_admin)
        response = self.client.post("/api/document/bulk-public/", {"slugs": [doc.slug], "is_public": True}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        doc.refresh_from_db()
        self.assertTrue(doc.is_public)

    def test_org_admin_does_not_see_other_clients(self):
        # Público: asignado a dos clientes, los dos lo ven (fase 0).
        skill = Skill.objects.create(
            name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"], scope="public"
        )
        self.caf.enabled_skills.add(skill)
        self.other.enabled_skills.add(skill)
        self._as(self.caf_admin)
        self.assertEqual(self.client.get("/api/skills/organizations/").status_code, status.HTTP_403_FORBIDDEN)
        listed = self.client.get(f"/api/skills/{skill.slug}/").data
        self.assertEqual(listed["organization_slugs"], [])

        self._as(self.ecofilia_admin)
        self.assertEqual(self.client.get("/api/skills/organizations/").status_code, status.HTTP_200_OK)
        listed = self.client.get(f"/api/skills/{skill.slug}/").data
        self.assertEqual(sorted(listed["organization_slugs"]), ["caf", "otro"])

    def test_org_admin_still_runs_assistants_in_their_operations(self):
        from apps.skill.access import user_can_run_assistants

        self.assertTrue(user_can_run_assistants(self.caf_admin))


class OrganizationIsolationReportTestCase(APITestCase):
    def setUp(self):
        caf = Organization.objects.create(name="CAF", slug="caf")
        other = Organization.objects.create(name="Otro", slug="otro")
        self.flagged = User.objects.create_user(email="raro@caf.com", password="x", organization=caf)
        User.objects.filter(pk=self.flagged.pk).update(is_staff=True)
        self.caf_member = User.objects.create_user(email="m@caf.com", password="x", organization=caf)
        outsider = User.objects.create_user(email="m@otro.com", password="x", organization=other)
        staff = User.objects.create_user(email="staff@ecofilia.site", password="x", is_staff=True)

        op = Project.objects.create(owner=self.caf_member, name="Operación")
        self.crossing = ProjectShare.objects.create(project=op, user=outsider)
        built_by_staff = Project.objects.create(owner=staff, name="Armada por Ecofilia")
        self.legit = ProjectShare.objects.create(project=built_by_staff, user=self.caf_member)

    def test_report_lists_without_touching(self):
        out = StringIO()
        call_command("organization_isolation_report", stdout=out)
        text = out.getvalue()
        self.assertIn("raro@caf.com (caf): staff", text)
        self.assertIn("m@otro.com (otro)", text)
        self.assertNotIn("Armada por Ecofilia", text)
        self.assertTrue(ProjectShare.objects.filter(pk=self.crossing.pk).exists())

    def test_revoke_only_removes_crossing_shares(self):
        call_command("organization_isolation_report", "--revoke-shares", stdout=StringIO())
        self.assertFalse(ProjectShare.objects.filter(pk=self.crossing.pk).exists())
        self.assertTrue(ProjectShare.objects.filter(pk=self.legit.pk).exists())
        self.flagged.refresh_from_db()
        self.assertTrue(self.flagged.is_staff, "los flags se corrigen desde el admin, no acá")
