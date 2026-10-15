"""
El administrador de una institución y la institución de cada operación.

- Una operación pertenece a una institución (``Project.organization``): la del
  dueño o, si la arma el equipo de Ecofilia, la del portal.
- El administrador de una institución (``role=admin`` con organización) ve y
  maneja todo lo de ella —operaciones, documentos de sus miembros, corridas—,
  y nada de afuera.
- Cada portal muestra sólo lo de su institución (``?organization=``).
"""
import importlib

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectShareRole
from apps.skill.access import user_can_mutate_execution
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillType
from apps.user.models import Organization

User = get_user_model()


class InstitutionAdminTestCase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", restricted=True)
        self.other = Organization.objects.create(name="Otro cliente", slug="otro")
        self.caf_admin = User.objects.create_user(email="admin@caf.com", password="x", organization=self.caf, role="admin")
        self.ana = User.objects.create_user(email="ana@caf.com", password="x", organization=self.caf)
        self.beto = User.objects.create_user(email="beto@caf.com", password="x", organization=self.caf)
        self.other_member = User.objects.create_user(email="m@otro.com", password="x", organization=self.other)
        self.other_admin = User.objects.create_user(email="admin@otro.com", password="x", organization=self.other, role="admin")
        self.staff = User.objects.create_user(email="staff@ecofilia.site", password="x", is_staff=True)

        self.op_ana = Project.objects.create(owner=self.ana, name="Operación de Ana")
        self.op_by_staff = Project.objects.create(owner=self.staff, name="Armada por Ecofilia", organization=self.caf)
        self.staff_own = Project.objects.create(owner=self.staff, name="Proyecto interno de Ecofilia")
        self.op_other = Project.objects.create(owner=self.other_member, name="Operación de otro cliente")

    def _slugs(self, user, **params):
        self.client.force_authenticate(user)
        response = self.client.get("/api/projects/", params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {p["slug"] for p in response.data}

    # ── institución de la operación ─────────────────────────────────────

    def test_operation_belongs_to_its_owners_institution(self):
        self.assertEqual(self.op_ana.organization, self.caf)
        self.assertIsNone(self.staff_own.organization)

    def test_creating_sets_the_institution(self):
        self.client.force_authenticate(self.ana)
        response = self.client.post("/api/projects/", {"name": "Nueva"}, format="json")
        self.assertEqual(response.data["organization_slug"], "caf")

        self.client.force_authenticate(self.staff)
        response = self.client.post("/api/projects/", {"name": "Desde el portal", "organization": "caf"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["organization_slug"], "caf")
        response = self.client.post("/api/projects/", {"name": "Interno"}, format="json")
        self.assertIsNone(response.data["organization_slug"])

    def test_nobody_creates_in_someone_elses_institution(self):
        self.client.force_authenticate(self.ana)
        response = self.client.post("/api/projects/", {"name": "x", "organization": "otro"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        outsider = User.objects.create_user(email="libre@example.com", password="x")
        self.client.force_authenticate(outsider)
        response = self.client.post("/api/projects/", {"name": "x", "organization": "caf"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_institution_does_not_change_after_creation(self):
        self.client.force_authenticate(self.staff)
        response = self.client.patch(f"/api/projects/{self.op_ana.slug}/", {"organization": "otro"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.op_ana.refresh_from_db()
        self.assertEqual(self.op_ana.organization, self.caf)

    # ── cada portal, sólo lo suyo ───────────────────────────────────────

    def test_portal_listing_only_brings_its_institution(self):
        self.assertEqual(self._slugs(self.staff, organization="caf"), {self.op_ana.slug, self.op_by_staff.slug})
        self.assertEqual(self._slugs(self.staff, organization="otro"), {self.op_other.slug})

    # ── administrador de la institución ─────────────────────────────────

    def test_admin_sees_every_operation_of_the_institution_and_nothing_else(self):
        self.assertEqual(self._slugs(self.caf_admin), {self.op_ana.slug, self.op_by_staff.slug})
        self.assertEqual(self._slugs(self.beto), set())
        self.assertEqual(self._slugs(self.other_admin), {self.op_other.slug})

    def test_admin_edits_shares_and_deletes_within_the_institution(self):
        self.client.force_authenticate(self.caf_admin)
        url = f"/api/projects/{self.op_ana.slug}/"
        detail = self.client.get(url).data
        self.assertTrue(detail["can_edit"] and detail["can_manage_shares"] and detail["can_delete"])
        self.assertEqual(self.client.patch(url, {"name": "Renombrada"}, format="json").status_code, status.HTTP_200_OK)

        shares = reverse("project-shares", kwargs={"slug": self.op_ana.slug})
        response = self.client.post(shares, {"user_email": self.beto.email, "role": "editor"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        self.assertEqual(self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT)

    def test_admin_has_nothing_outside(self):
        self.client.force_authenticate(self.caf_admin)
        self.assertEqual(self.client.get(f"/api/projects/{self.op_other.slug}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(f"/api/projects/{self.staff_own.slug}/").status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_manages_runs_of_the_institution(self):
        skill = Skill.objects.create(name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"])
        run = SkillExecution.objects.create(skill=skill, owner=self.ana, project=self.op_ana, status=ExecutionStatus.FAILED)
        self.assertTrue(user_can_mutate_execution(self.caf_admin, run))
        self.assertFalse(user_can_mutate_execution(self.other_admin, run))
        self.assertFalse(user_can_mutate_execution(self.beto, run))

        self.client.force_authenticate(self.caf_admin)
        self.assertTrue(self.client.get(f"/api/skills/executions/{run.pk}/").data["can_continue"])
        self.op_ana.shares.create(user=self.beto, role=ProjectShareRole.VIEWER)
        self.client.force_authenticate(self.beto)
        self.assertFalse(self.client.get(f"/api/skills/executions/{run.pk}/").data["can_continue"])

    def test_sharing_respects_the_operations_institution_even_for_staff(self):
        self.client.force_authenticate(self.staff)
        shares = reverse("project-shares", kwargs={"slug": self.op_by_staff.slug})
        rejected = self.client.post(shares, {"user_email": self.other_member.email, "role": "viewer"}, format="json")
        self.assertEqual(rejected.status_code, status.HTTP_400_BAD_REQUEST)
        accepted = self.client.post(shares, {"user_email": self.ana.email, "role": "viewer"}, format="json")
        self.assertEqual(accepted.status_code, status.HTTP_201_CREATED, accepted.data)

    # ── documentos ──────────────────────────────────────────────────────

    def test_admin_works_the_documents_of_its_members(self):
        mine = Document.objects.create(owner=self.ana, name="De Ana", slug="de-ana")
        public = Document.objects.create(owner=self.ana, name="Pública", slug="publica", is_public=True)
        foreign = Document.objects.create(owner=self.other_member, name="Ajeno", slug="ajeno")

        self.client.force_authenticate(self.caf_admin)
        detail = self.client.get(f"/api/document/{mine.slug}/")
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertTrue(detail.data["can_edit"])
        self.assertEqual(
            self.client.patch(f"/api/document/{mine.slug}/", {"name": "Clasificada"}, format="json").status_code,
            status.HTTP_200_OK,
        )
        # Lo público es de Ecofilia.
        self.assertFalse(self.client.get(f"/api/document/{public.slug}/").data["can_edit"])
        self.assertEqual(
            self.client.patch(f"/api/document/{public.slug}/", {"name": "x"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(self.client.get(f"/api/document/{foreign.slug}/").status_code, status.HTTP_404_NOT_FOUND)

        # Un miembro común no ve lo de sus colegas.
        self.client.force_authenticate(self.beto)
        self.assertEqual(self.client.get(f"/api/document/{mine.slug}/").status_code, status.HTTP_404_NOT_FOUND)

    def test_admin_bulk_tags_only_within_the_institution(self):
        tag, _ = EvidenceTag.objects.get_or_create(slug="ndc", defaults={"name": "NDC"})
        mine = Document.objects.create(owner=self.ana, name="De Ana", slug="de-ana")
        public = Document.objects.create(owner=self.ana, name="Pública", slug="publica", is_public=True)
        foreign = Document.objects.create(owner=self.other_member, name="Ajeno", slug="ajeno")

        self.client.force_authenticate(self.caf_admin)
        response = self.client.post(
            "/api/document/bulk-evidence-tags/",
            {"slugs": [mine.slug, public.slug, foreign.slug], "add": ["ndc"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["matched"], 1)
        self.assertTrue(mine.evidence_tags.filter(pk=tag.pk).exists())
        self.assertFalse(public.evidence_tags.exists())
        self.assertFalse(foreign.evidence_tags.exists())

        self.client.force_authenticate(self.beto)
        response = self.client.post("/api/document/bulk-evidence-tags/", {"slugs": [mine.slug], "add": ["ndc"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    # ── perfil ──────────────────────────────────────────────────────────

    def test_profile_says_which_admin_you_are(self):
        ecofilia_admin = User.objects.create_user(email="admin@ecofilia.site", password="x", role="admin")
        for user, ecofilia, institution in (
            (self.caf_admin, False, True),
            (ecofilia_admin, True, False),
            (self.ana, False, False),
        ):
            self.client.force_authenticate(user)
            me = self.client.get("/api/auth/me/").data
            self.assertEqual((me["is_ecofilia_admin"], me["is_organization_admin"]), (ecofilia, institution), user.email)


class BackfillProjectOrganizationTestCase(APITestCase):
    """La migración que fija la institución de las operaciones existentes."""

    def test_backfill(self):
        caf = Organization.objects.create(name="CAF", slug="caf")
        other = Organization.objects.create(name="Otro", slug="otro")
        staff = User.objects.create_user(email="staff@ecofilia.site", password="x", is_staff=True)
        member = User.objects.create_user(email="m@caf.com", password="x", organization=caf)
        iet = Skill.objects.create(name="IET", skill_type=SkillType.COPILOT, allowed_contexts=["project"])
        shared = Skill.objects.create(name="Compartido", skill_type=SkillType.COPILOT, allowed_contexts=["project"])
        caf.enabled_skills.add(iet, shared)
        other.enabled_skills.add(shared)

        by_member = Project.objects.create(owner=member, name="De un miembro")
        by_staff = Project.objects.create(owner=staff, name="Armada por staff")
        by_staff.enabled_skills.add(iet)
        ambiguous = Project.objects.create(owner=staff, name="Ambigua")
        ambiguous.enabled_skills.add(shared)
        internal = Project.objects.create(owner=staff, name="Interna")
        Project.objects.update(organization=None)

        migration = importlib.import_module("apps.project.migrations.0016_backfill_project_organization")
        migration.forwards(django_apps, None)

        for project, expected in ((by_member, caf), (by_staff, caf), (ambiguous, None), (internal, None)):
            project.refresh_from_db()
            self.assertEqual(project.organization, expected, project.name)
