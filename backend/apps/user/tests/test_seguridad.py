"""
Controles de seguridad antes de la salida a producción.

Roles, permisos y protección de la información. Cada clase cubre un control de
``backend/docs/seguridad.md``:
- autenticación sólo por JWT;
- bloqueo por intentos fallidos;
- MFA de un solo uso;
- tokens revocados al cambiar la contraseña;
- MFA para lo privilegiado;
- admin de Django;
- registro de auditoría;
- «público» que no sale de su institución;
- un barrido de aislamiento sobre todas las rutas de la API.
"""
import base64
from datetime import timedelta

import pyotp
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.chat.models import ChatSession
from apps.document.models import Category, Document
from apps.evaluation.models import Evaluation
from apps.project.models import Project, ProjectDocument
from apps.repository.models import Repository
from apps.skill.models import Skill, SkillExecution, SkillScope, SkillType
from apps.user.models import AuditEvent, Organization
from apps.user.tests.fixtures import caf_config, make_superadmin

User = get_user_model()
SECRET = "JBSWY3DPEHPK3PXP"


def _token(user) -> str:
    return str(RefreshToken.for_user(user).access_token)


def _code(offset_steps: int = 0) -> str:
    totp = pyotp.TOTP(SECRET)
    return totp.at(timezone.now().timestamp() + offset_steps * totp.interval)


class _Base(APITestCase):
    def setUp(self):
        # El throttle de anónimos (30/min) vive en el caché, que dura todo el
        # proceso de tests: sin esto, el resultado depende del orden.
        cache.clear()
        self.org_a = Organization.objects.create(name="A", slug="inst-a", config=caf_config())
        self.org_b = Organization.objects.create(name="B", slug="inst-b")
        self.member_a = User.objects.create_user(
            email="m@a.org", password="Clave-larga-2026", organization=self.org_a, email_verified=True
        )
        self.member_b = User.objects.create_user(
            email="m@b.org", password="Clave-larga-2026", organization=self.org_b, email_verified=True
        )
        self.public = User.objects.create_user(email="p@gmail.com", password="Clave-larga-2026", email_verified=True)
        self.superadmin = make_superadmin(email_verified=True)

    def _bearer(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {_token(user)}")

    def _login(self, email, password="Clave-larga-2026", otp=None):
        self.client.credentials()
        body = {"email": email, "password": password}
        if otp is not None:
            body["otp"] = otp
        return self.client.post("/api/auth/login/", body, format="json")


class OnlyJWTTestCase(_Base):
    def test_basic_auth_is_not_accepted(self):
        raw = base64.b64encode(b"m@a.org:Clave-larga-2026").decode()
        self.client.credentials(HTTP_AUTHORIZATION=f"Basic {raw}")
        self.assertEqual(self.client.get("/api/auth/me/").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_production_accepts_only_jwt(self):
        """Los tests corren con ``dev.py``, que suma la sesión para uso local; lo
        que hereda ``prod.py`` es ``base.py``."""
        from main.settings import base

        self.assertEqual(
            base.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"],
            ("apps.authentication.org_scope.OrgScopedJWTAuthentication",),
        )
        self.assertEqual(base.REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"], ("rest_framework.renderers.JSONRenderer",))
        self.assertTrue(base.SIMPLE_JWT["CHECK_REVOKE_TOKEN"])

    def test_the_browsable_login_does_not_exist_in_production(self):
        self.assertEqual(self.client.get("/api-auth/login/").status_code, status.HTTP_404_NOT_FOUND)

    def test_jwt_works(self):
        self._bearer(self.member_a)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, status.HTTP_200_OK)


@override_settings(LOGIN_MAX_FAILURES=3, LOGIN_LOCKOUT_MINUTES=15)
class LockoutTestCase(_Base):
    def test_locks_after_the_failures_and_unlocks_after_the_time(self):
        for _ in range(3):
            self.assertEqual(self._login("m@a.org", "mala").status_code, 401)
        locked = self._login("m@a.org")
        self.assertEqual(locked.status_code, 401)
        # El mismo error que una contraseña equivocada: no confirma que la cuenta existe.
        self.assertEqual(locked.data, self._login("nadie@x.org", "mala").data)
        User.objects.filter(pk=self.member_a.pk).update(locked_until=timezone.now() - timedelta(minutes=1))
        self.assertEqual(self._login("m@a.org").status_code, 200)
        self.member_a.refresh_from_db()
        self.assertEqual(self.member_a.failed_login_attempts, 0)

    def test_a_success_resets_the_counter(self):
        self._login("m@a.org", "mala")
        self._login("m@a.org", "mala")
        self.assertEqual(self._login("m@a.org").status_code, 200)
        self._login("m@a.org", "mala")
        self._login("m@a.org", "mala")
        self.assertEqual(self._login("m@a.org").status_code, 200)

    def test_mfa_failures_count_too(self):
        User.objects.filter(pk=self.member_a.pk).update(mfa_enabled=True, mfa_secret=SECRET)
        for _ in range(3):
            self.assertEqual(self._login("m@a.org", otp="000000").status_code, 401)
        self.assertEqual(self._login("m@a.org", otp=_code()).status_code, 401)


class MFATestCase(_Base):
    def setUp(self):
        super().setUp()
        User.objects.filter(pk=self.member_a.pk).update(mfa_enabled=True, mfa_secret=SECRET)

    def test_a_code_works_only_once(self):
        code = _code()
        self.assertEqual(self._login("m@a.org", otp=code).status_code, 200)
        self.assertEqual(self._login("m@a.org", otp=code).status_code, 401)

    def test_setup_cannot_turn_off_an_active_mfa(self):
        self._bearer(User.objects.get(pk=self.member_a.pk))
        response = self.client.post("/api/auth/mfa/setup/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.member_a.refresh_from_db()
        self.assertTrue(self.member_a.mfa_enabled)
        self.assertEqual(self.member_a.mfa_secret, SECRET)


class TokenRevocationTestCase(_Base):
    def test_changing_the_password_revokes_previous_tokens(self):
        self._bearer(self.member_a)
        response = self.client.post(
            "/api/auth/password/change/",
            {"old_password": "Clave-larga-2026", "new_password": "Otra-clave-larga-2027"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_deactivated_user_is_out_at_once(self):
        self._bearer(self.member_a)
        User.objects.filter(pk=self.member_a.pk).update(is_active=False)
        self.assertEqual(self.client.get("/api/auth/me/").status_code, status.HTTP_401_UNAUTHORIZED)


class PrivilegedNeedsMFATestCase(_Base):
    def setUp(self):
        super().setUp()
        self.no_mfa = User.objects.create_superuser(email="sin-mfa@ecofilia.site", password="x")
        Skill.objects.create(
            name="x", slug="x", skill_type=SkillType.QUICK, prompt_template="x", allowed_contexts=["document"]
        )
        Document.objects.create(owner=self.member_a, name="d", slug="d")

    def _calls(self):
        return [
            ("get", "/api/admin/overview/", {}),
            ("get", "/api/admin/audit/", {}),
            ("put", "/api/organizations/inst-b/config/", {"config": {}}),
            ("put", "/api/platform/config/public/", {"config": {}}),
            ("patch", "/api/skills/x/", {"scope": "public"}),
            ("post", "/api/document/bulk-public/", {"slugs": ["d"], "is_public": True}),
            ("post", "/api/document/evidence-tags/", {"name": "Global nueva"}),
        ]

    def test_a_superadmin_without_mfa_cannot(self):
        self.client.force_authenticate(self.no_mfa)
        for method, url, body in self._calls():
            response = getattr(self.client, method)(url, body, format="json")
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, (url, getattr(response, "data", None)))
            self.assertIn("MFA", str(response.data), url)
        self.assertFalse(Document.objects.get(slug="d").is_public)
        self.assertEqual(Skill.objects.get(slug="x").scope, SkillScope.PLATFORM)

    def test_with_mfa_it_can(self):
        self.client.force_authenticate(self.superadmin)
        for method, url, body in self._calls():
            response = getattr(self.client, method)(url, body, format="json")
            self.assertLess(response.status_code, 400, (url, getattr(response, "data", None)))

    @override_settings(ADMIN_REQUIRE_MFA=False)
    def test_the_requirement_can_be_turned_off(self):
        self.client.force_authenticate(self.no_mfa)
        self.assertEqual(self.client.get("/api/admin/overview/").status_code, status.HTTP_200_OK)


class DjangoAdminTestCase(_Base):
    def _post(self, email, otp=""):
        self.client.credentials()
        return self.client.post(
            "/admin/login/?next=/admin/", {"username": email, "password": "x", "otp": otp}, follow=False
        )

    def _logged_in(self):
        return "_auth_user_id" in self.client.session

    def test_staff_without_mfa_cannot_enter(self):
        User.objects.create_superuser(email="sin-mfa@ecofilia.site", password="x")
        self._post("sin-mfa@ecofilia.site")
        self.assertFalse(self._logged_in())

    def test_staff_with_mfa_needs_the_code(self):
        self._post("root@ecofilia.site", otp="000000")
        self.assertFalse(self._logged_in())
        response = self._post("root@ecofilia.site", otp=_code())
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self._logged_in())

    def test_an_institution_member_with_staff_cannot_enter(self):
        User.objects.filter(pk=self.member_a.pk).update(
            is_staff=True, mfa_enabled=True, mfa_secret=SECRET, password=User.objects.get(pk=self.superadmin.pk).password
        )
        self._post("m@a.org", otp=_code())
        self.assertFalse(self._logged_in())

    def test_the_mfa_secret_is_not_in_the_user_admin(self):
        self._post("root@ecofilia.site", otp=_code())
        response = self.client.get(f"/admin/user/user/{self.member_a.pk}/change/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="mfa_secret"')

    def test_the_audit_log_is_read_only_in_the_admin(self):
        AuditEvent.objects.create(action="x")
        self._post("root@ecofilia.site", otp=_code())
        self.assertEqual(self.client.get("/admin/user/auditevent/add/").status_code, 403)
        event = AuditEvent.objects.get()
        self.assertEqual(self.client.post(f"/admin/user/auditevent/{event.pk}/delete/", {"post": "yes"}).status_code, 403)


class AuditTestCase(_Base):
    def _actions(self):
        # Sin el rastro de acceso de cada pedido del superadmin (lo prueba
        # ``SuperadminAccessIsAuditedTestCase``).
        return list(
            AuditEvent.objects.exclude(action="superadmin.access").order_by("id").values_list("action", flat=True)
        )

    def test_privileged_actions_leave_a_trace(self):
        Skill.objects.create(
            name="x", slug="x", skill_type=SkillType.QUICK, prompt_template="x", allowed_contexts=["document"]
        )
        self.client.force_authenticate(self.superadmin)
        self.client.patch("/api/admin/organizations/inst-b/", {"restricted": True}, format="json")
        self.client.patch(f"/api/admin/users/{self.member_b.pk}/", {"role": "manager"}, format="json")
        self.client.post("/api/admin/skills/scope/", {"slugs": ["x"], "scope": "public"}, format="json")
        self.assertEqual(
            self._actions(), ["admin.organization.updated", "admin.user.updated", "skill.scope.changed"]
        )
        event = AuditEvent.objects.get(action="admin.organization.updated")
        self.assertEqual(
            (event.actor_email, event.organization, event.data),
            ("root@ecofilia.site", self.org_b, {"before": {"restricted": False}, "after": {"restricted": True}}),
        )
        rows = self.client.get("/api/admin/audit/?action=admin.").data
        self.assertEqual([r["action"] for r in rows], ["admin.user.updated", "admin.organization.updated"])

    def test_logins_leave_a_trace(self):
        self._login("m@a.org", "mala")
        self._login("m@a.org")
        self.assertEqual(self._actions(), ["auth.login.failure", "auth.login.success"])
        self.assertEqual(AuditEvent.objects.get(action="auth.login.success").actor, self.member_a)

    def test_it_cannot_be_edited_or_deleted(self):
        event = AuditEvent.objects.create(action="x")
        event.action = "y"
        with self.assertRaises(PermissionError):
            event.save()
        with self.assertRaises(PermissionError):
            event.delete()

    def test_an_action_that_fails_leaves_no_trace(self):
        self.client.force_authenticate(self.superadmin)
        self.client.patch("/api/admin/organizations/inst-b/", {"enabled_features": ["nada"]}, format="json")
        self.assertEqual(self._actions(), [])


class PublicStaysInItsInstitutionTestCase(_Base):
    def test_a_member_cannot_publish_an_evaluation(self):
        self._bearer(self.member_a)
        response = self.client.post(
            "/api/evaluations/", {"title": "Mía", "visibility": "public"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, getattr(response, "data", None))
        self.assertFalse(Evaluation.objects.filter(title="Mía").exists())

    def test_old_public_evaluations_of_a_member_do_not_leave_the_institution(self):
        evaluation = Evaluation.objects.create(owner=self.member_a, title="Vieja pública", visibility="public")
        colleague = User.objects.create_user(email="c@a.org", password="x", organization=self.org_a)
        for user, visible in ((colleague, True), (self.member_b, False), (self.public, False)):
            self.assertEqual(Evaluation.objects.for_user(user).filter(pk=evaluation.pk).exists(), visible, user.email)
            self.assertEqual(evaluation.can_view(user), visible, user.email)

    def test_a_superadmin_publication_reaches_everyone(self):
        self.client.force_authenticate(self.superadmin)
        response = self.client.post("/api/evaluations/", {"title": "Plataforma", "visibility": "public"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        evaluation = Evaluation.objects.get(title="Plataforma")
        for user in (self.member_a, self.member_b, self.public):
            self.assertTrue(Evaluation.objects.for_user(user).filter(pk=evaluation.pk).exists(), user.email)
        self.assertTrue(AuditEvent.objects.filter(action="evaluation.visibility.changed").exists())

    def test_a_public_repository_of_a_member_does_not_leave_the_institution(self):
        repo = Repository.objects.create(owner=self.member_a, name="Repo A", repo_type="public")
        self.assertFalse(Repository.objects.for_user(self.member_b).filter(pk=repo.pk).exists())
        self.assertFalse(repo.can_view(self.member_b))
        self.assertTrue(Repository.objects.for_user(self.member_a).filter(pk=repo.pk).exists())


class CrossTenantSweepTestCase(_Base):
    """Todas las rutas GET de la API sin parámetros de ruta, como A: nada de B.

    Es un barrido, no una lista: una ruta nueva entra sola, y si filtra datos
    de otra institución, este test la encuentra.
    """

    MARKER = "zz-secreto-de-b"

    def setUp(self):
        super().setUp()
        m = self.MARKER
        op = Project.objects.create(owner=self.member_b, name=f"Op {m}", context_notes={"pais": m})
        doc = Document.objects.create(owner=self.member_b, name=f"Doc {m}", slug=f"doc-{m}", topics=[m])
        ProjectDocument.objects.create(project=op, document=doc)
        skill = Skill.objects.create(
            name=f"Skill {m}", slug=f"skill-{m}", skill_type=SkillType.QUICK, prompt_template="x",
            allowed_contexts=["project"], scope=SkillScope.ORGANIZATION, organization=self.org_b,
        )
        SkillExecution.objects.create(skill=skill, owner=self.member_b, project=op, output=m)
        Evaluation.objects.create(owner=self.member_b, title=f"Eval {m}", visibility="public")
        Repository.objects.create(owner=self.member_b, name=f"Repo {m}", repo_type="public")
        ChatSession.objects.create(owner=self.member_b, title=f"Chat {m}")
        Category.objects.create(owner=self.member_b, name=f"Cat {m}")
        from apps.document.models import EvidenceTag

        EvidenceTag.objects.create(slug=f"tag-{m}", name=f"Tag {m}", organization=self.org_b)

    def _list_routes(self):
        out = []

        def walk(patterns, prefix=""):
            for p in patterns:
                if isinstance(p, URLResolver):
                    walk(p.url_patterns, prefix + str(p.pattern))
                elif isinstance(p, URLPattern):
                    route = (prefix + str(p.pattern)).replace("^", "").replace("$", "").replace("\\.", ".")
                    if "<" in route or "(?P" in route or "format" in route or not route.startswith("api/"):
                        continue
                    out.append("/" + route)

        walk(get_resolver().url_patterns)
        return sorted(set(out))

    def test_no_route_shows_b_to_a_or_to_the_public(self):
        routes = self._list_routes()
        self.assertGreater(len(routes), 25)
        for user in (self.member_a, self.public):
            self._bearer(user)
            for route in routes:
                response = self.client.get(route, {"organization": "inst-a"})
                body = response.content.decode(errors="ignore")
                self.assertNotIn(self.MARKER, body, (user.email, route, response.status_code))
        # Y el barrido sí ve lo de B cuando mira B: si no, no prueba nada.
        self._bearer(self.member_b)
        self.assertIn(self.MARKER, self.client.get("/api/projects/").content.decode())


class DeletingAnInstitutionTestCase(_Base):
    """Punto 1 del front: borrar una institución volvía superadmins a sus admins."""

    def test_an_institution_with_members_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        admin_b = User.objects.create_user(email="admin@b.org", password="x", organization=self.org_b, role="admin")
        with self.assertRaises(ProtectedError):
            self.org_b.delete()
        admin_b.refresh_from_db()
        self.assertEqual(admin_b.organization, self.org_b)

    def test_an_institution_with_operations_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        empty = Organization.objects.create(name="Vacía", slug="vacia")
        Project.objects.create(owner=self.superadmin, organization=empty, name="Op")
        with self.assertRaises(ProtectedError):
            empty.delete()

    def test_losing_the_institution_never_makes_a_superadmin(self):
        from apps.user.access import is_ecofilia_admin

        admin_b = User.objects.create_user(email="admin@b.org", password="x", organization=self.org_b, role="admin")
        admin_b.organization = None
        admin_b.save()
        admin_b.refresh_from_db()
        self.assertEqual(admin_b.role, "member")
        self.assertFalse(is_ecofilia_admin(admin_b))
        # También con update_fields parcial (el admin de Django, un script).
        admin_b.organization, admin_b.role = self.org_b, "admin"
        admin_b.save()
        admin_b.organization = None
        admin_b.save(update_fields=["organization"])
        admin_b.refresh_from_db()
        self.assertEqual(admin_b.role, "member")

    def test_an_empty_institution_can_still_be_deleted(self):
        empty = Organization.objects.create(name="Vacía", slug="vacia")
        empty.delete()
        self.assertFalse(Organization.objects.filter(slug="vacia").exists())


class LeavingAnInstitutionTestCase(_Base):
    """Punto 3 del front: quien sale de una institución seguía viendo lo que creó ahí."""

    def setUp(self):
        super().setUp()
        self.op = Project.objects.create(owner=self.member_a, name="Op de A")
        self.doc = Document.objects.create(owner=self.member_a, name="Doc de A", slug="doc-de-a")
        ProjectDocument.objects.create(project=self.op, document=self.doc)
        skill = Skill.objects.create(
            name="s", slug="s", skill_type=SkillType.QUICK, prompt_template="x", allowed_contexts=["project"],
            scope=SkillScope.PUBLIC,
        )
        self.run = SkillExecution.objects.create(skill=skill, owner=self.member_a, project=self.op, output="secreto")
        self.chat = ChatSession.objects.create(owner=self.member_a, title="Chat de A", project=self.op)
        self.evaluation = Evaluation.objects.create(owner=self.member_a, title="Eval de A", project=self.op)
        self.own_doc = Document.objects.create(owner=self.member_a, name="Propio", slug="propio")

    def _leave(self, to=None):
        User.objects.filter(pk=self.member_a.pk).update(organization=to)
        self.member_a.refresh_from_db()

    def _sees(self):
        self._bearer(self.member_a)
        return {
            "operación": self.client.get(f"/api/projects/{self.op.slug}/").status_code,
            "corrida": self.client.get(f"/api/skills/executions/{self.run.pk}/").status_code,
            "documento": self.client.get(f"/api/document/{self.doc.slug}/").status_code,
            "chat": self.client.get(f"/api/chat/sessions/{self.chat.pk}/").status_code,
            "evaluación": self.client.get(f"/api/evaluations/{self.evaluation.slug}/").status_code,
        }

    def test_while_in_the_institution_they_see_everything(self):
        self.assertEqual(set(self._sees().values()), {200})

    def test_after_leaving_to_no_institution_they_see_nothing_of_it(self):
        self._leave(None)
        self.assertEqual(set(self._sees().values()), {404}, self._sees())
        # Lo suyo que no es de la institución sigue siendo suyo.
        self.assertEqual(self.client.get("/api/document/propio/").status_code, 200)

    def test_after_moving_to_another_institution_they_see_nothing_of_it(self):
        self._leave(self.org_b)
        self.assertEqual(set(self._sees().values()), {404}, self._sees())
        listing = self.client.get("/api/skills/executions/?view=summary").content.decode()
        self.assertNotIn(f'"id":{self.run.pk}', listing.replace(" ", ""))


class MFAIsMandatoryForSuperadminsTestCase(_Base):
    """Punto 5 del front: sin MFA, un superadmin no tiene ningún poder de plataforma."""

    def setUp(self):
        super().setUp()
        self.no_mfa = User.objects.create_superuser(email="sin-mfa@ecofilia.site", password="x", email_verified=True)
        self.op_b = Project.objects.create(owner=self.member_b, name="Op de B")

    def test_without_mfa_a_superadmin_is_a_regular_user(self):
        self._bearer(self.no_mfa)
        me = self.client.get("/api/auth/me/").data
        self.assertEqual(
            (me["is_ecofilia_admin"], me["is_superuser"], me["mfa_required"], me["home_path"]),
            (False, False, True, "/app"),
        )
        self.assertEqual(self.client.get(f"/api/projects/{self.op_b.slug}/").status_code, 404)
        self.assertEqual(self.client.get("/api/admin/overview/").status_code, 403)
        # Puede activar el MFA.
        self.assertEqual(self.client.post("/api/auth/mfa/setup/", {}, format="json").status_code, 201)

    def test_the_login_says_the_same(self):
        response = self._login("sin-mfa@ecofilia.site", "x")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((response.data["user"]["is_ecofilia_admin"], response.data["user"]["mfa_required"]), (False, True))

    def test_with_mfa_a_superadmin_sees_everything(self):
        self._bearer(self.superadmin)
        me = self.client.get("/api/auth/me/").data
        self.assertEqual((me["is_ecofilia_admin"], me["mfa_required"], me["home_path"]), (True, False, "/admin"))
        self.assertEqual(self.client.get(f"/api/projects/{self.op_b.slug}/").status_code, 200)

    def test_nothing_is_persisted(self):
        self._bearer(self.no_mfa)
        self.client.patch("/api/auth/me/", {"first_name": "Ana"}, format="json")
        self.no_mfa.refresh_from_db()
        self.assertTrue(self.no_mfa.is_superuser)
        self.assertEqual(self.no_mfa.first_name, "Ana")


class SuperadminAccessIsAuditedTestCase(_Base):
    """Punto 4 del front: qué lee el superadmin."""

    def test_every_superadmin_request_leaves_a_trace(self):
        op = Project.objects.create(owner=self.member_b, name="Op de B")
        self._bearer(self.superadmin)
        self.client.get(f"/api/projects/{op.slug}/")
        self.client.get("/api/projects/?organization=inst-b")
        events = list(AuditEvent.objects.filter(action="superadmin.access").order_by("id"))
        self.assertEqual([e.data["path"] for e in events], [f"/api/projects/{op.slug}/", "/api/projects/"])
        self.assertEqual((events[1].organization, events[1].data["status"], events[1].actor), (self.org_b, 200, self.superadmin))

    def test_regular_users_are_not_logged_per_request(self):
        self._bearer(self.member_a)
        self.client.get("/api/projects/")
        self.assertFalse(AuditEvent.objects.filter(action="superadmin.access").exists())


class PasswordResetKeepsMFATestCase(_Base):
    """Revisión del front: el restablecer apagaba el MFA. Con el email de
    alguien se le cambiaba la clave *y* se le sacaba el segundo factor."""

    def test_the_reset_does_not_turn_off_mfa(self):
        from apps.authentication.services.tokens import TokenService

        User.objects.filter(pk=self.member_a.pk).update(mfa_enabled=True, mfa_secret=SECRET)
        user = User.objects.get(pk=self.member_a.pk)
        response = self.client.post(
            "/api/auth/password/reset/confirm/",
            {
                "uid": TokenService.make_uid(user),
                "token": TokenService.generate_token(user),
                "new_password": "Clave-nueva-2027",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        user.refresh_from_db()
        self.assertTrue(user.check_password("Clave-nueva-2027"))
        self.assertTrue(user.mfa_enabled)
        # Con la clave nueva sola no alcanza: sigue pidiendo el código.
        self.assertEqual(self._login("m@a.org", password="Clave-nueva-2027").status_code, 401)
        self.assertEqual(self._login("m@a.org", password="Clave-nueva-2027", otp=_code()).status_code, 200)


class MFASecretIsEncryptedTestCase(_Base):
    """Revisión del front: el secreto TOTP estaba en claro en la base."""

    def test_setup_stores_it_encrypted_and_it_still_verifies(self):
        from apps.authentication.services.secret_box import is_encrypted

        self._bearer(self.member_a)
        response = self.client.post("/api/auth/mfa/setup/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        secret = response.data["secret"]
        stored = User.objects.get(pk=self.member_a.pk).mfa_secret
        self.assertTrue(is_encrypted(stored))
        self.assertNotIn(secret, stored)

        response = self.client.post("/api/auth/mfa/verify/", {"code": pyotp.TOTP(secret).now()}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(User.objects.get(pk=self.member_a.pk).mfa_enabled)

    def test_an_old_plaintext_secret_keeps_working(self):
        User.objects.filter(pk=self.member_a.pk).update(mfa_enabled=True, mfa_secret=SECRET)
        self.assertEqual(self._login("m@a.org", otp=_code()).status_code, 200)

    def test_the_migration_encrypts_and_reverts(self):
        import importlib

        from django.apps import apps as registry

        from apps.authentication.services.secret_box import decrypt, is_encrypted

        migration = importlib.import_module("apps.user.migrations.0014_cifrar_secretos_mfa")
        User.objects.filter(pk=self.member_a.pk).update(mfa_enabled=True, mfa_secret=SECRET)

        migration.cifrar(registry, None)
        stored = User.objects.get(pk=self.member_a.pk).mfa_secret
        self.assertTrue(is_encrypted(stored))
        self.assertEqual(decrypt(stored), SECRET)
        migration.cifrar(registry, None)  # idempotente: no lo cifra dos veces
        self.assertEqual(User.objects.get(pk=self.member_a.pk).mfa_secret, stored)
        self.assertEqual(self._login("m@a.org", otp=_code()).status_code, 200)

        migration.descifrar(registry, None)
        self.assertEqual(User.objects.get(pk=self.member_a.pk).mfa_secret, SECRET)

    def test_keys_rotate(self):
        from cryptography.fernet import Fernet

        from apps.authentication.services.secret_box import decrypt, encrypt

        old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
        with self.settings(MFA_ENCRYPTION_KEYS=[old]):
            stored = encrypt(SECRET)
        with self.settings(MFA_ENCRYPTION_KEYS=[new, old]):
            self.assertEqual(decrypt(stored), SECRET)
        with self.settings(MFA_ENCRYPTION_KEYS=[new]), self.assertRaises(Exception):
            decrypt(stored)


class StaffIsNotGlobalTestCase(_Base):
    """Revisión del front: ``is_staff`` sin ser superadmin veía todo."""

    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(
            email="staff@ecofilia.site", password="Clave-larga-2026", email_verified=True,
            is_staff=True, mfa_enabled=True, mfa_secret=SECRET,
        )
        self.op_b = Project.objects.create(owner=self.member_b, name="Op de B")

    def test_staff_sees_nothing_of_the_institutions(self):
        self._bearer(self.staff)
        self.assertEqual(self.client.get(f"/api/projects/{self.op_b.slug}/").status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.get("/api/projects/")
        data = response.data
        slugs = [p["slug"] for p in (data["results"] if isinstance(data, dict) else data)]
        self.assertNotIn(self.op_b.slug, slugs)
        self.assertEqual(self.client.get("/api/admin/organizations/").status_code, status.HTTP_403_FORBIDDEN)

    def test_the_login_profile_says_the_same(self):
        response = self._login("staff@ecofilia.site", otp=_code())
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual((response.data["user"]["is_ecofilia_admin"], response.data["user"]["home_path"]), (False, "/app"))

    def test_a_superadmin_keeps_global_access(self):
        self._bearer(self.superadmin)
        self.assertEqual(self.client.get(f"/api/projects/{self.op_b.slug}/").status_code, status.HTTP_200_OK)

    def test_nothing_is_persisted(self):
        self._bearer(self.staff)
        self.client.patch("/api/auth/me/", {"first_name": "Sol"}, format="json")
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.is_staff)
        self.assertEqual(self.staff.first_name, "Sol")
