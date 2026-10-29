"""
Aislamiento entre instituciones (fase 0).

Dos instituciones (A, sin restricciones, y B, restringida), un usuario público
sin institución y un superadmin de Ecofilia. Se recorren los listados y los
detalles de todo lo que una institución tiene —asistentes, corridas,
operaciones, documentos, etiquetas de evidencia, informes emitidos y registro
de evaluación— y se verifica:

- A nunca ve nada de B, ni B de A;
- el público no ve nada de ninguna institución ni los asistentes de plataforma;
- el superadmin ve todo.

Los pedidos van con JWT de verdad, no con ``force_authenticate``: el alcance de
un miembro de institución se aplica en la autenticación (ver
``apps.user.access.scope_to_organization``).
"""
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.document.models import Document, EvidenceTag
from apps.document.services import visible_documents_q
from apps.project.models import (
    Project,
    ProjectDocument,
    ProjectStageReport,
    ProjectStageReportOrigin,
)
from apps.project.services.evidence_tags import tagged_in_project_q
from apps.skill.models import ExecutionStatus, Skill, SkillExecution, SkillScope, SkillType
from apps.user.models import Organization
from apps.user.tests.fixtures import caf_config

User = get_user_model()


def _rows(data):
    return data["results"] if isinstance(data, dict) and "results" in data else data


class AislamientoEntreInstitucionesTestCase(APITestCase):
    @classmethod
    def setUpTestData(cls):
        # Con los criterios de CAF, para que el registro de evaluación tenga qué
        # mostrar: desde la fase 1 los criterios salen de la configuración.
        cls.org_a = Organization.objects.create(name="Institución A", slug="inst-a", config=caf_config())
        cls.org_b = Organization.objects.create(
            name="Institución B", slug="inst-b", restricted=True, config=caf_config()
        )

        cls.member_a = User.objects.create_user(email="m@a.org", password="x", organization=cls.org_a)
        cls.admin_a = User.objects.create_user(
            email="admin@a.org", password="x", organization=cls.org_a, role="admin"
        )
        cls.member_b = User.objects.create_user(email="m@b.org", password="x", organization=cls.org_b)
        cls.public = User.objects.create_user(email="persona@gmail.com", password="x")
        cls.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")

        # ── Asistentes ────────────────────────────────────────────────────
        def skill(slug, scope, organization=None, owner=None):
            return Skill.objects.create(
                name=slug,
                slug=slug,
                skill_type=SkillType.QUICK,
                prompt_template="{{context}}",
                allowed_contexts=["project", "document"],
                scope=scope,
                organization=organization,
                owner=owner,
                members_can_run=True,
            )

        cls.skill_platform = skill("de-plataforma", SkillScope.PLATFORM)
        cls.skill_public = skill("abierta", SkillScope.PUBLIC)
        cls.skill_a = skill("de-a", SkillScope.ORGANIZATION, cls.org_a)
        cls.skill_b = skill("de-b", SkillScope.ORGANIZATION, cls.org_b)
        # B es restringida: de las suyas sólo ve las que Ecofilia le habilitó.
        cls.skill_b_off = skill("de-b-sin-habilitar", SkillScope.ORGANIZATION, cls.org_b)
        cls.org_b.enabled_skills.add(cls.skill_b)
        cls.skill_mine_a = skill("propia-de-a", SkillScope.USER, owner=cls.member_a)
        cls.skill_mine_public = skill("propia-del-publico", SkillScope.USER, owner=cls.public)

        # ── Operaciones y documentos ──────────────────────────────────────
        cls.op_a = Project.objects.create(owner=cls.member_a, name="Operación A")
        cls.op_b = Project.objects.create(owner=cls.member_b, name="Operación B")
        cls.op_public = Project.objects.create(owner=cls.public, name="Operación pública")

        def doc(owner, slug, project=None):
            document = Document.objects.create(
                owner=owner, name=slug, slug=slug, chunking_status="done", extracted_text="x"
            )
            if project is not None:
                ProjectDocument.objects.create(project=project, document=document)
            return document

        cls.doc_a = doc(cls.member_a, "doc-a", cls.op_a)
        cls.doc_b = doc(cls.member_b, "doc-b", cls.op_b)
        cls.doc_public = doc(cls.public, "doc-del-publico", cls.op_public)
        cls.doc_library = doc(cls.superadmin, "biblioteca-publica")
        cls.doc_library.is_public = True
        cls.doc_library.save(update_fields=["is_public"])

        # ── Corridas ──────────────────────────────────────────────────────
        def run(skill_, owner, project, status_=ExecutionStatus.COMPLETED):
            return SkillExecution.objects.create(
                skill=skill_, owner=owner, project=project, status=status_
            )

        cls.exec_a = run(cls.skill_a, cls.member_a, cls.op_a)
        cls.exec_b = run(cls.skill_b, cls.member_b, cls.op_b)
        cls.exec_public = run(cls.skill_public, cls.public, cls.op_public)

        # ── Etiquetas ─────────────────────────────────────────────────────
        cls.tag_global, _ = EvidenceTag.objects.update_or_create(
            slug="ndc", organization=None, defaults={"name": "NDC"}
        )
        cls.tag_a = EvidenceTag.objects.create(slug="propia-a", name="Propia A", organization=cls.org_a)
        cls.tag_b = EvidenceTag.objects.create(slug="propia-b", name="Propia B", organization=cls.org_b)
        # El mismo slug en las dos instituciones: cada una ve la suya.
        cls.shared_a = EvidenceTag.objects.create(slug="mismo-slug", name="De A", organization=cls.org_a)
        cls.shared_b = EvidenceTag.objects.create(slug="mismo-slug", name="De B", organization=cls.org_b)
        cls.doc_a.evidence_tags.add(cls.tag_a, cls.shared_a)
        cls.doc_b.evidence_tags.add(cls.tag_b, cls.shared_b)

        # ── Informes emitidos ─────────────────────────────────────────────
        def emitted(project, owner):
            return ProjectStageReport.objects.create(
                project=project,
                stage="ido_cci",
                origin=ProjectStageReportOrigin.EDITADO,
                created_by=owner,
                emitted_at=timezone.now(),
                emitted_by=owner,
                emitted_markdown="# Informe",
                emitted_assessment={"determinaciones": {"ct_a1": "Consistente"}},
            )

        cls.report_a = emitted(cls.op_a, cls.member_a)
        cls.report_b = emitted(cls.op_b, cls.member_b)

    def _as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def _get(self, url, expected=status.HTTP_200_OK):
        response = self.client.get(url)
        self.assertEqual(response.status_code, expected, getattr(response, "data", None))
        return response

    # ── Asistentes ────────────────────────────────────────────────────────

    def _skill_slugs(self):
        return {s["slug"] for s in _rows(self._get("/api/skills/").data)}

    def test_skills_list_member_of_a(self):
        self._as(self.member_a)
        slugs = self._skill_slugs()
        self.assertEqual(
            slugs & {"de-plataforma", "abierta", "de-a", "de-b", "de-b-sin-habilitar",
                     "propia-de-a", "propia-del-publico"},
            {"abierta", "de-a", "propia-de-a"},
        )

    def test_skills_list_member_of_restricted_b(self):
        self._as(self.member_b)
        slugs = self._skill_slugs()
        self.assertIn("de-b", slugs)
        self.assertIn("abierta", slugs)
        for hidden in ("de-plataforma", "de-a", "de-b-sin-habilitar", "propia-de-a", "propia-del-publico"):
            self.assertNotIn(hidden, slugs)

    def test_skills_list_public_user(self):
        self._as(self.public)
        slugs = self._skill_slugs()
        self.assertEqual(
            slugs & {"de-plataforma", "abierta", "de-a", "de-b", "de-b-sin-habilitar",
                     "propia-de-a", "propia-del-publico"},
            {"abierta", "propia-del-publico"},
        )

    def test_skills_list_superadmin_sees_all(self):
        self._as(self.superadmin)
        slugs = self._skill_slugs()
        self.assertTrue(
            {"de-plataforma", "abierta", "de-a", "de-b", "de-b-sin-habilitar",
             "propia-de-a", "propia-del-publico"} <= slugs
        )

    def test_skill_detail_and_run_of_other_institution_is_404(self):
        cases = [
            (self.member_a, ["de-b", "de-b-sin-habilitar", "de-plataforma", "propia-del-publico"]),
            (self.member_b, ["de-a", "de-b-sin-habilitar", "de-plataforma", "propia-de-a"]),
            (self.public, ["de-a", "de-b", "de-plataforma", "propia-de-a"]),
        ]
        for user, hidden in cases:
            self._as(user)
            for slug in hidden:
                self._get(f"/api/skills/{slug}/", status.HTTP_404_NOT_FOUND)
                response = self.client.post(
                    reverse("skill-run", kwargs={"slug": slug}),
                    {"context_type": "document", "context_slug": "doc-a"},
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, (user.email, slug))

    def test_skill_detail_exposes_scope_and_organization(self):
        self._as(self.member_a)
        data = self._get("/api/skills/de-a/").data
        self.assertEqual((data["scope"], data["organization_slug"]), ("organization", "inst-a"))
        data = self._get("/api/skills/abierta/").data
        self.assertEqual((data["scope"], data["organization_slug"]), ("public", None))
        self._as(self.superadmin)
        data = self._get("/api/skills/de-b/").data
        self.assertEqual((data["scope"], data["organization_slug"]), ("organization", "inst-b"))

    def test_continuing_a_run_of_a_skill_you_do_not_see_is_403(self):
        """Corrida propia, asistente ajeno: reanudar, repetir o avanzar es usarlo."""
        execution = SkillExecution.objects.create(
            skill=self.skill_a, owner=self.public, project=self.op_public,
            status=ExecutionStatus.FAILED,
        )
        self._as(self.public)
        for action in ("resume", "rerun", "rerun-from-step", "approve", "regenerate-step"):
            response = self.client.post(
                f"/api/skills/executions/{execution.pk}/{action}/", {"step_title": "x"}, format="json"
            )
            self.assertIn(
                response.status_code, (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND), action
            )
            execution.refresh_from_db()
            self.assertEqual(execution.status, ExecutionStatus.FAILED, action)

    # ── Corridas ──────────────────────────────────────────────────────────

    def _execution_ids(self):
        return {e["id"] for e in _rows(self._get("/api/skills/executions/?view=summary").data)}

    def test_executions(self):
        mine = {
            self.member_a: self.exec_a,
            self.member_b: self.exec_b,
            self.public: self.exec_public,
        }
        everyone = {e.pk for e in mine.values()}
        for user, own in mine.items():
            self._as(user)
            ids = self._execution_ids()
            self.assertEqual(ids & everyone, {own.pk}, user.email)
            for other in everyone - {own.pk}:
                self._get(f"/api/skills/executions/{other}/", status.HTTP_404_NOT_FOUND)
        self._as(self.superadmin)
        self.assertTrue(everyone <= self._execution_ids())

    # ── Operaciones ───────────────────────────────────────────────────────

    def test_operations(self):
        ops = {self.member_a: self.op_a, self.member_b: self.op_b, self.public: self.op_public}
        everyone = {op.slug for op in ops.values()}
        for user, own in ops.items():
            self._as(user)
            slugs = {p["slug"] for p in _rows(self._get("/api/projects/").data)}
            self.assertEqual(slugs & everyone, {own.slug}, user.email)
            for other in everyone - {own.slug}:
                self._get(f"/api/projects/{other}/", status.HTTP_404_NOT_FOUND)
        # El administrador de A ve todas las de A y ninguna otra.
        self._as(self.admin_a)
        slugs = {p["slug"] for p in _rows(self._get("/api/projects/").data)}
        self.assertEqual(slugs & everyone, {self.op_a.slug})
        self._as(self.superadmin)
        slugs = {p["slug"] for p in _rows(self._get("/api/projects/").data)}
        self.assertTrue(everyone <= slugs)

    # ── Documentos ────────────────────────────────────────────────────────

    def test_documents(self):
        docs = {self.member_a: self.doc_a, self.member_b: self.doc_b, self.public: self.doc_public}
        everyone = {d.slug for d in docs.values()}
        for user, own in docs.items():
            self._as(user)
            slugs = {d["slug"] for d in _rows(self._get("/api/document/list/?scope=all").data)}
            self.assertEqual(slugs & everyone, {own.slug}, user.email)
            for other in everyone - {own.slug}:
                self._get(f"/api/document/{other}/", status.HTTP_404_NOT_FOUND)
            # La biblioteca pública es de todos.
            self._get(f"/api/document/{self.doc_library.slug}/")
        self._as(self.superadmin)
        slugs = {d["slug"] for d in _rows(self._get("/api/document/list/?scope=all").data)}
        self.assertTrue(everyone <= slugs)
        for slug in everyone:
            self._get(f"/api/document/{slug}/")

    def test_visible_documents_q(self):
        everyone = {self.doc_a.pk, self.doc_b.pk, self.doc_public.pk}
        expected = {
            self.member_a: {self.doc_a.pk},
            self.admin_a: {self.doc_a.pk},
            self.member_b: {self.doc_b.pk},
            self.public: {self.doc_public.pk},
        }
        for user, own in expected.items():
            visible = set(
                Document.objects.filter(visible_documents_q(user)).values_list("pk", flat=True)
            )
            self.assertEqual(visible & everyone, own, user.email)
            self.assertIn(self.doc_library.pk, visible)

    # ── Etiquetas de evidencia ────────────────────────────────────────────

    def _tags(self, url="/api/document/evidence-tags/"):
        return {(t["slug"], t["organization_slug"]) for t in _rows(self._get(url).data)}

    def test_evidence_tags(self):
        own_tags = {("propia-a", "inst-a"), ("propia-b", "inst-b"),
                    ("mismo-slug", "inst-a"), ("mismo-slug", "inst-b")}
        expected = {
            self.member_a: {("propia-a", "inst-a"), ("mismo-slug", "inst-a")},
            self.member_b: {("propia-b", "inst-b"), ("mismo-slug", "inst-b")},
            self.public: set(),
        }
        for user, own in expected.items():
            self._as(user)
            tags = self._tags()
            self.assertIn(("ndc", None), tags, user.email)
            self.assertEqual(tags & own_tags, own, user.email)
        self._as(self.superadmin)
        self.assertTrue(own_tags | {("ndc", None)} <= self._tags())
        filtered = self._tags("/api/document/evidence-tags/?organization=inst-a")
        self.assertEqual(filtered & own_tags, {("propia-a", "inst-a"), ("mismo-slug", "inst-a")})
        self.assertIn(("ndc", None), filtered)

    def test_evidence_tag_detail_and_document_count(self):
        self._as(self.member_a)
        self._get("/api/document/evidence-tags/propia-b/", status.HTTP_404_NOT_FOUND)
        data = self._get("/api/document/evidence-tags/mismo-slug/").data
        self.assertEqual((data["name"], data["organization_slug"]), ("De A", "inst-a"))
        # Cuenta sólo los documentos que ve: el de B no suma.
        self.assertEqual(data["document_count"], 1)

    def test_step_tags_resolve_within_the_operation_institution(self):
        """Un paso que pide «mismo-slug» en una operación de A lee el de A, nunca el de B."""
        # Un documento de B mal vinculado a la operación de A, con la etiqueta de B.
        ProjectDocument.objects.create(project=self.op_a, document=self.doc_b)
        matched = set(
            Document.objects.filter(tagged_in_project_q(self.op_a.pk, ["mismo-slug"]))
            .values_list("pk", flat=True)
        )
        self.assertEqual(matched, {self.doc_a.pk})
        matched = set(
            Document.objects.filter(tagged_in_project_q(self.op_a.pk, ["propia-b"]))
            .values_list("pk", flat=True)
        )
        self.assertEqual(matched, set())

    # ── Informes emitidos ─────────────────────────────────────────────────

    def _emitted_projects(self, url="/api/projects/stage-reports/emitidos/"):
        return {r["project_slug"] for r in _rows(self._get(url).data)}

    def test_emitted_reports(self):
        both = {self.op_a.slug, self.op_b.slug}
        expected = {
            self.member_a: {self.op_a.slug},
            self.admin_a: {self.op_a.slug},
            self.member_b: {self.op_b.slug},
            self.public: set(),
        }
        for user, own in expected.items():
            self._as(user)
            self.assertEqual(self._emitted_projects() & both, own, user.email)
            # Pedir la carpeta de otra institución no trae nada.
            other = "inst-b" if own == {self.op_a.slug} else "inst-a"
            self.assertEqual(
                self._emitted_projects(f"/api/projects/stage-reports/emitidos/?organization={other}") & both,
                set(),
                user.email,
            )
        self._as(self.superadmin)
        self.assertEqual(self._emitted_projects() & both, both)

    # ── Registro de evaluación ────────────────────────────────────────────

    def _records(self, query, expected=status.HTTP_200_OK):
        response = self.client.get(f"/api/projects/evaluation-records/{query}")
        self.assertEqual(response.status_code, expected, getattr(response, "data", None))
        return {r["project_slug"] for r in response.data} if expected == status.HTTP_200_OK else None

    def test_evaluation_records_require_the_institution(self):
        for user in (self.member_a, self.public, self.superadmin):
            self._as(user)
            self._records("", status.HTTP_400_BAD_REQUEST)

    def test_evaluation_records_only_of_your_institution(self):
        self._as(self.member_a)
        self.assertEqual(self._records("?organization=inst-a"), {self.op_a.slug})
        self._records("?organization=inst-b", status.HTTP_403_FORBIDDEN)
        self._as(self.member_b)
        self.assertEqual(self._records("?organization=inst-b"), {self.op_b.slug})
        self._records("?organization=inst-a", status.HTTP_403_FORBIDDEN)
        self._as(self.public)
        self._records("?organization=inst-a", status.HTTP_403_FORBIDDEN)
        self._records("?organization=inst-b", status.HTTP_403_FORBIDDEN)

    def test_evaluation_records_superadmin_sees_each_institution(self):
        self._as(self.superadmin)
        self.assertEqual(self._records("?organization=inst-a"), {self.op_a.slug})
        self.assertEqual(self._records("?organization=inst-b"), {self.op_b.slug})
        self._records("?organization=no-existe", status.HTTP_404_NOT_FOUND)

    # ── Una barrida final: nada de B en ninguna respuesta de A ────────────

    def test_nothing_of_b_leaks_into_any_listing_of_a(self):
        markers = [
            self.op_b.slug, self.doc_b.slug, "de-b", "de-b-sin-habilitar", "propia-b", "De B",
        ]
        urls = [
            "/api/skills/",
            "/api/skills/executions/",
            "/api/projects/",
            "/api/document/list/?scope=all",
            "/api/document/evidence-tags/",
            "/api/projects/stage-reports/emitidos/",
        ]
        for user in (self.member_a, self.admin_a, self.public):
            self._as(user)
            own_records = (
                ["/api/projects/evaluation-records/?organization=inst-a"] if user.organization_id else []
            )
            for url in urls + own_records:
                body = self._get(url).content.decode()
                for marker in markers:
                    self.assertNotIn(f'"{marker}"', body, (user.email, url, marker))


class AlcanceDeEtiquetasYSkillsQueSeCreanTestCase(APITestCase):
    """Lo que crea cada uno queda dentro de su alcance."""

    def setUp(self):
        self.org_a = Organization.objects.create(name="Institución A", slug="inst-a")
        self.member_a = User.objects.create_user(email="m@a.org", password="x", organization=self.org_a)
        self.public = User.objects.create_user(email="persona@gmail.com", password="x")
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")

    def _as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_a_tag_created_by_a_member_belongs_to_their_institution(self):
        self._as(self.member_a)
        response = self.client.post("/api/document/evidence-tags/", {"name": "Informe propio"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        tag = EvidenceTag.objects.get(slug="informe-propio")
        self.assertEqual(tag.organization, self.org_a)

    def test_a_member_cannot_create_a_tag_for_another_institution(self):
        Organization.objects.create(name="Institución B", slug="inst-b")
        self._as(self.member_a)
        response = self.client.post(
            "/api/document/evidence-tags/",
            {"name": "Ajena", "organization_slug": "inst-b"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_public_user_cannot_add_to_the_catalog(self):
        self._as(self.public)
        response = self.client.post("/api/document/evidence-tags/", {"name": "Global"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(EvidenceTag.objects.filter(slug="global").exists())

    def test_superadmin_creates_global_or_institution_tags(self):
        self._as(self.superadmin)
        response = self.client.post("/api/document/evidence-tags/", {"name": "Nueva global"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(EvidenceTag.objects.get(slug="nueva-global").organization_id)
        response = self.client.post(
            "/api/document/evidence-tags/",
            {"name": "De A", "organization_slug": "inst-a"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(EvidenceTag.objects.get(slug="de-a").organization, self.org_a)

    def test_an_institution_tag_cannot_shadow_a_global_one(self):
        EvidenceTag.objects.update_or_create(slug="ndc", organization=None, defaults={"name": "NDC"})
        self._as(self.member_a)
        response = self.client.post("/api/document/evidence-tags/", {"name": "NDC"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_skill_created_by_a_member_is_theirs(self):
        self._as(self.member_a)
        response = self.client.post(
            "/api/skills/",
            {
                "name": "Mi skill",
                "skill_type": "quick",
                "allowed_contexts": ["document"],
                "prompt_template": "{{context}}",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        skill = Skill.objects.get(name="Mi skill")
        self.assertEqual((skill.scope, skill.owner, skill.organization), (SkillScope.USER, self.member_a, None))

    def test_a_member_cannot_choose_another_scope_when_creating(self):
        self._as(self.member_a)
        response = self.client.post(
            "/api/skills/",
            {
                "name": "Para todos",
                "skill_type": "quick",
                "allowed_contexts": ["document"],
                "prompt_template": "{{context}}",
                "scope": "public",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Skill.objects.filter(name="Para todos").exists())
        self.assertFalse(Skill.objects.filter(Q(scope="public") & Q(owner=self.member_a)).exists())
