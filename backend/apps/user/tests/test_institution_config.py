"""
La configuración de cada institución, en datos (fase 1).

Contrato en ``backend/docs/contrato-instituciones.md``. Lo que se prueba:
- la lectura efectiva;
- la validación de escritura;
- los endpoints;
- que las etapas, los lanzamientos, la etiqueta principal, los grupos y los
  criterios salgan de la configuración y no del código.
"""
import importlib
from unittest.mock import patch

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.document.models import Document, EvidenceTag
from apps.project.models import Project, ProjectDocument
from apps.project.services.evaluation_records import records_for
from apps.project.services.stages import IET_SLUG, stage_workflow_slugs
from apps.skill.models import Skill, SkillScope, SkillType
from apps.user import institution_config
from apps.user.models import Organization
from apps.user.tests.fixtures import caf_config

User = get_user_model()
DELAY = "apps.skill.dispatch.run_skill_task.delay"




def make_caf(**extra) -> Organization:
    """Una CAF como la de producción: su configuración y sus skills de institución."""
    caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config(), **extra)
    Skill.objects.filter(slug__startswith="caf-").update(scope=SkillScope.ORGANIZATION, organization=caf)
    return caf


def other_config(skill_slug: str, tag_slug: str) -> dict:
    return {
        "version": 1,
        "branding": {"display_name": "Otra institución", "logo_url": ""},
        "stages": [
            {"slug": "borrador", "label": "Borrador", "principal_tag": None, "launches": []},
            {"slug": "revision", "label": "En revisión", "principal_tag": None, "launches": [skill_slug]},
        ],
        "workflows": {"report": None, "guided": None, "reading": None},
        "evidence_tag_groups": [{"slug": "propias", "label": "Propias", "tags": [tag_slug]}],
        "evaluation": {"framework": "", "criteria": []},
    }


class EffectiveConfigTestCase(TestCase):
    def test_without_institution_is_the_base(self):
        config = institution_config.effective_config(None)
        self.assertEqual(config["stages"], [])
        self.assertEqual(config["workflows"], {"report": None, "guided": None, "reading": None})
        self.assertEqual(institution_config.tag_group(config, "ndc"), "contexto")
        self.assertEqual(institution_config.tag_group(config, "ido"), "otro")

    def test_an_institution_without_config_takes_the_base_with_its_name(self):
        org = Organization.objects.create(name="Banco X", slug="banco-x")
        config = institution_config.effective_config(org)
        self.assertEqual(config["branding"]["display_name"], "Banco X")
        self.assertEqual(config["stages"], [])

    def test_missing_top_level_keys_come_from_the_base(self):
        org = Organization.objects.create(
            name="Banco X", slug="banco-x", config={"version": 1, "stages": [{"slug": "a", "label": "A"}]}
        )
        config = institution_config.effective_config(org)
        self.assertEqual(institution_config.stage_slugs(config), ["a"])
        self.assertEqual(institution_config.tag_group(config, "ndc"), "contexto")

    def test_caf_config_reproduces_what_the_code_had(self):
        config = caf_config()
        self.assertEqual(
            institution_config.stage_slugs(config), ["pre_originacion", "ido_cci", "dec", "aprobada"]
        )
        self.assertEqual(institution_config.report_stages(config), ["ido_cci"])
        self.assertEqual(institution_config.principal_tag(config, "ido_cci"), "ido")
        self.assertEqual(institution_config.principal_tag(config, "dec"), "dec")
        self.assertEqual(institution_config.tag_group(config, "ias"), "operacion")
        # Los criterios coinciden con los de las reglas de determinación.
        from apps.project.services.caf_criteria import CRITERIOS

        self.assertEqual(
            [(c["key"], f"{c['code']} — {c['name']}") for c in institution_config.criteria(config)],
            CRITERIOS,
        )


class ValidateConfigTestCase(TestCase):
    def setUp(self):
        self.caf = make_caf()
        self.other = Organization.objects.create(name="Otra", slug="otra")

    def test_caf_config_is_valid_for_caf(self):
        config, errors = institution_config.validate_config(caf_config(), self.caf)
        self.assertEqual(errors, [])
        self.assertEqual(config, caf_config())

    def test_caf_workflows_are_not_usable_by_another_institution(self):
        _, errors = institution_config.validate_config(caf_config(), self.other)
        self.assertTrue(any("stages[1].launches[0]" in e and IET_SLUG in e for e in errors), errors)
        self.assertTrue(any(e.startswith("workflows.report") for e in errors), errors)

    def test_collects_every_error_with_its_path(self):
        Skill.objects.create(
            name="Plat", slug="de-plataforma", skill_type=SkillType.QUICK, prompt_template="x",
            allowed_contexts=["project"],
        )
        bad = caf_config()
        bad["extra"] = 1
        bad["stages"].append({"slug": "ido_cci", "label": ""})
        bad["stages"][0]["launches"] = ["de-plataforma"]
        bad["stages"][0]["principal_tag"] = "no-existe"
        bad["evidence_tag_groups"][1]["tags"].append("ido")
        bad["evaluation"]["criteria"][0]["options"][0]["tone"] = "rojo"
        bad["evaluation"]["criteria"].pop()
        _, errors = institution_config.validate_config(bad, self.caf)
        expected = [
            "config: claves desconocidas: extra",
            "stages[4].slug: «ido_cci» está repetida",
            "stages[4].label",
            "stages[0].launches[0]",
            "stages[0].principal_tag",
            "evidence_tag_groups[1].tags[7]: «ido» ya está en el grupo «principal»",
            "evaluation.criteria[0].options[0].tone",
            "evaluation.criteria: el marco «caf-datbc» exige",
        ]
        for fragment in expected:
            self.assertTrue(any(fragment in e for e in errors), (fragment, errors))

    def test_not_an_object(self):
        self.assertEqual(institution_config.validate_config([], self.caf)[1], ["config: tiene que ser un objeto."])


class OrganizationConfigAPITestCase(APITestCase):
    def setUp(self):
        self.caf = make_caf(restricted=True)
        self.other = Organization.objects.create(name="Otra", slug="otra")
        self.caf_member = User.objects.create_user(email="m@caf.org", password="x", organization=self.caf)
        self.other_member = User.objects.create_user(email="m@otra.org", password="x", organization=self.other)
        self.public = User.objects.create_user(email="persona@gmail.com", password="x")
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")

    def _as(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def test_me(self):
        self._as(self.caf_member)
        data = self.client.get("/api/organizations/me/config/").data
        self.assertEqual(data["organization"], {"slug": "caf", "name": "CAF", "restricted": True})
        self.assertEqual(data["config"], caf_config())

        self._as(self.other_member)
        data = self.client.get("/api/organizations/me/config/").data
        self.assertEqual(data["organization"]["slug"], "otra")
        self.assertEqual(data["config"]["stages"], [])
        self.assertEqual(data["config"]["branding"]["display_name"], "Otra")

        for user in (self.public, self.superadmin):
            self._as(user)
            data = self.client.get("/api/organizations/me/config/").data
            self.assertIsNone(data["organization"])
            self.assertEqual(data["config"], institution_config.BASE_CONFIG)

    def test_reading_another_institution_is_404(self):
        cases = {
            self.caf_member: {"caf": 200, "otra": 404, "no-existe": 404},
            self.other_member: {"caf": 404, "otra": 200},
            self.public: {"caf": 404, "otra": 404},
            self.superadmin: {"caf": 200, "otra": 200, "no-existe": 404},
        }
        for user, expected in cases.items():
            self._as(user)
            for slug, code in expected.items():
                response = self.client.get(f"/api/organizations/{slug}/config/")
                self.assertEqual(response.status_code, code, (user.email, slug))

    def test_superadmin_replaces_it(self):
        skill = Skill.objects.create(
            name="De otra", slug="de-otra", skill_type=SkillType.QUICK, prompt_template="x",
            allowed_contexts=["project"], scope=SkillScope.ORGANIZATION, organization=self.other,
        )
        tag = EvidenceTag.objects.create(slug="propia", name="Propia", organization=self.other)
        self._as(self.superadmin)
        response = self.client.put(
            "/api/organizations/otra/config/", {"config": other_config(skill.slug, tag.slug)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.other.refresh_from_db()
        self.assertEqual(institution_config.stage_slugs(self.other.config), ["borrador", "revision"])
        self.assertEqual(response.data["config"]["stages"][1]["launches"], ["de-otra"])

    def test_an_invalid_config_is_400_and_changes_nothing(self):
        self._as(self.superadmin)
        response = self.client.put("/api/organizations/otra/config/", {"config": caf_config()}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(response.data["config"])
        self.other.refresh_from_db()
        self.assertEqual(self.other.config, {})

    def test_only_superadmin_writes(self):
        self._as(self.caf_member)
        response = self.client.put("/api/organizations/caf/config/", {"config": caf_config()}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.put("/api/organizations/otra/config/", {"config": {}}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self._as(self.public)
        response = self.client.put("/api/organizations/caf/config/", {"config": {}}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_tag_groups_come_from_the_viewer_institution(self):
        EvidenceTag.objects.update_or_create(slug="ias", organization=None, defaults={"name": "IAS"})
        self._as(self.caf_member)
        groups = {t["slug"]: t["group"] for t in self.client.get("/api/document/evidence-tags/").data}
        self.assertEqual((groups["ias"], groups["ndc"]), ("operacion", "contexto"))
        self._as(self.other_member)
        groups = {t["slug"]: t["group"] for t in self.client.get("/api/document/evidence-tags/").data}
        # «otra» no tiene configuración propia: rige la base, que no conoce «ias».
        self.assertEqual((groups["ias"], groups["ndc"]), ("otro", "contexto"))
        self._as(self.superadmin)
        groups = {
            t["slug"]: t["group"]
            for t in self.client.get("/api/document/evidence-tags/?organization=caf").data
        }
        self.assertEqual(groups["ias"], "operacion")


class StagesFromConfigTestCase(APITestCase):
    def setUp(self):
        self.caf = make_caf()
        self.other = Organization.objects.create(name="Otra", slug="otra")
        self.other_skill = Skill.objects.create(
            name="De otra", slug="de-otra", skill_type=SkillType.COPILOT, allowed_contexts=["project"],
            scope=SkillScope.ORGANIZATION, organization=self.other,
        )
        tag = EvidenceTag.objects.create(slug="propia", name="Propia", organization=self.other)
        self.other.config = other_config(self.other_skill.slug, tag.slug)
        self.other.save()
        self.superadmin = User.objects.create_superuser(email="root@ecofilia.site", password="x")

    def _project(self, organization, estado):
        owner = User.objects.create_user(
            email=f"o{Project.objects.count()}@x.org", password="x", organization=organization
        )
        project = Project.objects.create(
            owner=owner, organization=organization, name="Op", context_notes={"estado": estado}
        )
        doc = Document.objects.create(owner=owner, name="Principal", slug=f"p-{project.pk}",
                                      chunking_status="done", extracted_text="x")
        ProjectDocument.objects.create(project=project, document=doc, is_primary=True)
        project.blueprint_document = doc
        project.save(update_fields=["blueprint_document"])
        return project

    def _move(self, project, to):
        self.client.force_authenticate(self.superadmin)
        with patch(DELAY), self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"/api/projects/{project.slug}/stage/", {"to": to}, format="json")

    def test_caf_launches_the_iet_in_ido(self):
        project = self._project(self.caf, "pre_originacion")
        self.assertEqual(stage_workflow_slugs(project, "ido_cci"), [IET_SLUG])
        response = self._move(project, "ido_cci")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_another_institution_has_its_own_stages_and_launches(self):
        project = self._project(self.other, "borrador")
        self.assertEqual(stage_workflow_slugs(project, "ido_cci"), [])
        self.assertEqual(stage_workflow_slugs(project, "revision"), ["de-otra"])
        self.assertEqual(self._move(project, "ido_cci").status_code, status.HTTP_400_BAD_REQUEST)
        response = self._move(project, "revision")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        project.refresh_from_db()
        self.assertEqual(project.context_notes["estado"], "revision")
        launches = project.stage_transitions.get(to_stage="revision").launches
        self.assertEqual([entry["skill_slug"] for entry in launches], ["de-otra"])

    def test_an_operation_without_institution_launches_nothing_of_caf(self):
        project = self._project(None, "")
        self.assertEqual(stage_workflow_slugs(project, "ido_cci"), [])
        self.assertEqual(self._move(project, "ido_cci").status_code, status.HTTP_400_BAD_REQUEST)

    def test_principal_tag_comes_from_the_stage(self):
        from apps.project.services.evidence_tags import effective_tag_slugs, ensure_principal_stage_tag

        EvidenceTag.objects.update_or_create(slug="ido", organization=None, defaults={"name": "IDO"})
        project = self._project(self.caf, "ido_cci")
        self.assertEqual(ensure_principal_stage_tag(project), "ido")
        link = ProjectDocument.objects.get(project=project, document=project.blueprint_document)
        self.assertIn("ido", effective_tag_slugs(link))
        other = self._project(self.other, "revision")
        self.assertEqual(ensure_principal_stage_tag(other), "")

    def test_evaluation_records_use_the_institution_criteria(self):
        caf_op = self._project(self.caf, "ido_cci")
        other_op = self._project(self.other, "revision")
        self.assertEqual(records_for([other_op]), [])
        from apps.project.services.evaluation_records import criterio_keys, evaluation_stages

        self.assertEqual(criterio_keys(caf_op)[0], "ct_a1")
        self.assertEqual(evaluation_stages(caf_op), ["ido_cci", "dec"])


class CafConfigMigrationTestCase(TestCase):
    def test_applies_only_to_caf_and_reverts(self):
        migration = importlib.import_module("apps.user.migrations.0009_config_caf")
        migration.aplicar(django_apps, None)  # sin CAF: no hace nada
        caf = Organization.objects.create(name="CAF", slug="caf")
        other = Organization.objects.create(name="Otra", slug="otra")
        migration.aplicar(django_apps, None)
        caf.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(caf.config, caf_config())
        self.assertEqual(other.config, {})
        migration.revertir(django_apps, None)
        caf.refresh_from_db()
        self.assertEqual(caf.config, {})
