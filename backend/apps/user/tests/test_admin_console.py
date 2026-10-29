"""
La consola del superadmin (fase 2). Contrato en ``backend/docs/contrato-instituciones.md``.
"""
from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import EvidenceTag
from apps.project.models import Project
from apps.skill.models import Skill, SkillScope, SkillType
from apps.user.models import Organization
from apps.user.tests.fixtures import caf_config

User = get_user_model()


def _skill(slug, **extra):
    return Skill.objects.create(
        name=slug, slug=slug, skill_type=SkillType.QUICK, prompt_template="x",
        allowed_contexts=["project"], **extra,
    )


class _ConsoleBase(APITestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", restricted=True, config=caf_config())
        self.other = Organization.objects.create(name="Otra", slug="otra")
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")
        self.caf_admin = User.objects.create_user(
            email="admin@caf.org", password="x", organization=self.caf, role="admin"
        )
        self.member = User.objects.create_user(email="m@caf.org", password="x", organization=self.caf)
        self.public = User.objects.create_user(email="persona@gmail.com", password="x")
        Project.objects.create(owner=self.member, name="Op CAF")
        self.caf_skill = _skill("de-caf", scope=SkillScope.ORGANIZATION, organization=self.caf)
        self.public_skill = _skill("abierta", scope=SkillScope.PUBLIC)
        self.platform_skill = _skill("de-plataforma")
        self.other_skill = _skill("de-otra", scope=SkillScope.ORGANIZATION, organization=self.other)
        self.client.force_authenticate(self.superadmin)


class OnlySuperadminTestCase(_ConsoleBase):
    def test_everyone_else_gets_403(self):
        calls = [
            ("get", "/api/admin/overview/"),
            ("get", "/api/admin/organizations/"),
            ("get", "/api/admin/organizations/caf/"),
            ("patch", "/api/admin/organizations/caf/"),
            ("get", "/api/admin/users/"),
            ("patch", f"/api/admin/users/{self.member.pk}/"),
            ("post", "/api/admin/skills/scope/"),
        ]
        for user in (self.caf_admin, self.member, self.public):
            self.client.force_authenticate(user)
            for method, url in calls:
                response = getattr(self.client, method)(url, {}, format="json")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, (user.email, url))


class OverviewAndOrganizationsTestCase(_ConsoleBase):
    def test_overview(self):
        data = self.client.get("/api/admin/overview/").data
        self.assertEqual(data["levels"], {"superadmin": 1, "organization": 2, "public": 1})
        self.assertGreaterEqual(data["skills_by_scope"]["organization"], 2)
        caf = next(o for o in data["organizations"] if o["slug"] == "caf")
        self.assertEqual(
            (caf["members_count"], caf["operations_count"], caf["own_skills_count"], caf["has_config"]),
            (2, 1, 1, True),
        )

    def test_detail(self):
        self.caf.enabled_skills.add(self.caf_skill)
        data = self.client.get("/api/admin/organizations/caf/").data
        self.assertEqual(data["enabled_skill_slugs"], ["de-caf"])
        self.assertEqual(data["default_skill_slugs"], [])
        self.assertEqual(self.client.get("/api/admin/organizations/no-existe/").status_code, 404)

    def test_patch(self):
        response = self.client.patch(
            "/api/admin/organizations/caf/",
            {
                "name": "CAF — Banco de Desarrollo",
                "restricted": False,
                "enabled_features": ["projects", "chat"],
                "enabled_skill_slugs": ["de-caf", "abierta"],
                "default_skill_slugs": ["de-caf"],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.caf.refresh_from_db()
        self.assertEqual(self.caf.name, "CAF — Banco de Desarrollo")
        self.assertFalse(self.caf.restricted)
        self.assertEqual(self.caf.enabled_features, ["projects", "chat"])
        self.assertEqual(response.data["enabled_skill_slugs"], ["abierta", "de-caf"])
        self.assertEqual(response.data["default_skill_slugs"], ["de-caf"])

    def test_patch_rejects_what_breaks_isolation_or_is_unknown(self):
        cases = [
            ({"enabled_features": ["projects", "nada"]}, "enabled_features"),
            ({"enabled_skill_slugs": ["de-otra"]}, "enabled_skill_slugs"),
            ({"enabled_skill_slugs": ["de-plataforma"]}, "enabled_skill_slugs"),
            ({"default_skill_slugs": ["abierta"]}, "default_skill_slugs"),
            ({"name": "Otra"}, "name"),
            ({"slug": "nuevo"}, "detail"),
        ]
        for payload, field in cases:
            response = self.client.patch("/api/admin/organizations/caf/", payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)
            self.assertIn(field, response.data, payload)
        self.caf.refresh_from_db()
        self.assertEqual((self.caf.slug, self.caf.name), ("caf", "CAF"))


class UsersTestCase(_ConsoleBase):
    def _emails(self, query=""):
        return [u["email"] for u in self.client.get(f"/api/admin/users/{query}").data]

    def test_list_and_filters(self):
        rows = {u["email"]: u for u in self.client.get("/api/admin/users/").data}
        self.assertEqual(rows["m@caf.org"]["level"], "organization")
        self.assertEqual(rows["m@caf.org"]["organization_slug"], "caf")
        self.assertEqual(rows["m@caf.org"]["operations_count"], 1)
        self.assertEqual(rows["root@ecofilia.site"]["level"], "superadmin")
        self.assertEqual(rows["persona@gmail.com"]["level"], "public")
        self.assertEqual(self._emails("?level=public"), ["persona@gmail.com"])
        self.assertEqual(self._emails("?organization=caf"), ["admin@caf.org", "m@caf.org"])
        self.assertEqual(self._emails("?q=persona"), ["persona@gmail.com"])
        self.assertEqual(self.client.get("/api/admin/users/?level=otro").status_code, 400)

    def test_patch_moves_a_user_but_not_their_operations(self):
        op = Project.objects.get(owner=self.member)
        response = self.client.patch(
            f"/api/admin/users/{self.member.pk}/", {"organization_slug": "otra", "role": "admin"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.data["organization_slug"], response.data["role"]), ("otra", "admin"))
        op.refresh_from_db()
        self.assertEqual(op.organization, self.caf)
        # Sin institución y con role=admin sería superadmin: hay que bajarle el rol a la vez.
        response = self.client.patch(
            f"/api/admin/users/{self.member.pk}/", {"organization_slug": None}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.patch(
            f"/api/admin/users/{self.member.pk}/", {"organization_slug": None, "role": "member"}, format="json"
        )
        self.assertEqual(response.data["level"], "public")

    def test_the_console_never_creates_a_superadmin(self):
        for user, payload in (
            (self.caf_admin, {"organization_slug": None}),
            (self.public, {"role": "admin"}),
        ):
            response = self.client.patch(f"/api/admin/users/{user.pk}/", payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)
            user.refresh_from_db()
            self.assertNotEqual(
                (user.organization_id, user.role), (None, "admin"), payload
            )

    def test_patch_guards(self):
        staff = User.objects.create_user(email="staff@ecofilia.site", password="x", is_staff=True)
        cases = [
            (staff, {"organization_slug": "caf"}, "organization_slug"),
            (self.superadmin, {"is_active": False}, "detail"),
            (self.member, {"organization_slug": "no-existe"}, "organization_slug"),
            (self.member, {"role": "dios"}, "role"),
            (self.member, {"email": "x@x.org"}, "detail"),
        ]
        for user, payload, field in cases:
            response = self.client.patch(f"/api/admin/users/{user.pk}/", payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)
            self.assertIn(field, response.data, payload)
        self.assertEqual(self.client.patch("/api/admin/users/999999/", {}, format="json").status_code, 404)


class SkillsAndTagsTestCase(_ConsoleBase):
    def test_skill_list_filters(self):
        slugs = {s["slug"] for s in self.client.get("/api/skills/?scope=public").data}
        self.assertIn("abierta", slugs)
        self.assertNotIn("de-caf", slugs)
        slugs = {s["slug"] for s in self.client.get("/api/skills/?organization=caf").data}
        self.assertEqual(slugs, {"de-caf"})

    def test_bulk_scope(self):
        response = self.client.post(
            "/api/admin/skills/scope/", {"slugs": ["de-plataforma", "de-otra"], "scope": "public"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual({r["scope"] for r in response.data}, {"public"})
        self.other_skill.refresh_from_db()
        self.assertEqual((self.other_skill.scope, self.other_skill.organization_id), ("public", None))

        response = self.client.post(
            "/api/admin/skills/scope/",
            {"slugs": ["abierta"], "scope": "organization", "organization_slug": "caf"},
            format="json",
        )
        self.assertEqual(response.data, [{"slug": "abierta", "scope": "organization", "organization_slug": "caf"}])

    def test_bulk_scope_is_all_or_nothing(self):
        response = self.client.post(
            "/api/admin/skills/scope/", {"slugs": ["de-plataforma", "no-existe"], "scope": "public"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.platform_skill.refresh_from_db()
        self.assertEqual(self.platform_skill.scope, "platform")
        response = self.client.post(
            "/api/admin/skills/scope/", {"slugs": ["de-plataforma"], "scope": "organization"}, format="json"
        )
        self.assertIn("organization_slug", response.data)

    def test_move_a_tag_between_institutions(self):
        tag = EvidenceTag.objects.create(slug="propia", name="Propia", organization=self.caf)
        EvidenceTag.objects.create(slug="choca", name="Choca", organization=self.other)
        response = self.client.patch(
            "/api/document/evidence-tags/propia/?organization=caf", {"organization_slug": "otra"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        tag.refresh_from_db()
        self.assertEqual(tag.organization, self.other)

        clash = EvidenceTag.objects.create(slug="choca", name="Choca CAF", organization=self.caf)
        response = self.client.patch(
            "/api/document/evidence-tags/choca/?organization=caf", {"organization_slug": "otra"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, response.data)
        clash.refresh_from_db()
        self.assertEqual(clash.organization, self.caf)

    def test_a_member_cannot_move_a_tag(self):
        EvidenceTag.objects.create(slug="propia", name="Propia", organization=self.caf)
        self.client.force_authenticate(self.caf_admin)
        response = self.client.patch(
            "/api/document/evidence-tags/propia/", {"organization_slug": "otra"}, format="json"
        )
        self.assertIn(response.status_code, (status.HTTP_400_BAD_REQUEST, status.HTTP_403_FORBIDDEN))
        self.assertEqual(EvidenceTag.objects.get(slug="propia").organization, self.caf)
