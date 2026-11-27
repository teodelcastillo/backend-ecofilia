"""
Una skill eliminada no queda en lo que lanzan las etapas.

Las configuraciones nombran skills por slug, sin clave foránea. Así quedó
``caf-enverdecimiento-operacion`` en los ``launches`` de CAF tras eliminarse
(27/11/2026): cada cambio de etapa la intentaba lanzar y la consola no podía
volver a guardar la configuración. Se prueba:
- la migración que la saca (``user/0020``);
- que eliminar una skill la saque de toda configuración guardada;
- que no se pueda eliminar la que es informe de una etapa o un rol de workflows.
"""
import copy
import importlib

from django.apps import apps as django_apps
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.skill.models import Skill, SkillScope, SkillType
from apps.user import institution_config
from apps.user.models import AuditEvent, Organization, PlatformConfig
from apps.user.tests.fixtures import caf_config, make_superadmin

ENVERDECIMIENTO = "caf-enverdecimiento-operacion"
IET = "caf-iet-datbc-evaluacion-tecnica"


def _launches(config: dict, stage: str) -> list[str]:
    return next(s for s in config["stages"] if s["slug"] == stage)["launches"]


class MigracionSinEnverdecimientoTestCase(TestCase):
    def test_lo_saca_de_instituciones_y_plataforma_y_deja_lo_demas(self):
        config = caf_config()
        _launches(config, "ido_cci").append(ENVERDECIMIENTO)
        caf = Organization.objects.create(name="CAF", slug="caf", config=config)
        intacta = Organization.objects.create(name="Otra", slug="otra", config=caf_config())
        plantilla = PlatformConfig.objects.create(key="template", config=copy.deepcopy(config))

        importlib.import_module("apps.user.migrations.0020_sin_enverdecimiento_en_etapas").aplicar(
            django_apps, None
        )

        for fila in (caf, plantilla):
            fila.refresh_from_db()
            self.assertEqual(_launches(fila.config, "ido_cci"), [IET])
        intacta.refresh_from_db()
        self.assertEqual(intacta.config, caf_config())


class EliminarSkillTestCase(APITestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Otra", slug="otra")
        self.lanzada = self._skill("lanzada")
        self.informe = self._skill("informe")
        self.org.config = {
            "version": 1,
            "branding": {"display_name": "Otra", "logo_url": ""},
            "stages": [
                {"slug": "borrador", "label": "Borrador", "principal_tag": None,
                 "launches": [self.lanzada.slug]},
                {"slug": "revision", "label": "Revisión", "principal_tag": None,
                 "launches": [self.lanzada.slug, self.informe.slug], "report": self.informe.slug},
            ],
            "workflows": {"report": None, "guided": None, "reading": None},
            "evidence_tag_groups": [],
            "evaluation": {"framework": "", "criteria": []},
        }
        self.org.save()
        self.plantilla = PlatformConfig.objects.create(key="template", config=copy.deepcopy(self.org.config))
        self.client.force_authenticate(make_superadmin())

    def _skill(self, slug):
        return Skill.objects.create(
            name=slug, slug=slug, skill_type=SkillType.COPILOT, allowed_contexts=["project"],
            scope=SkillScope.ORGANIZATION, organization=self.org,
        )

    def test_eliminarla_la_saca_de_las_etapas(self):
        response = self.client.delete(f"/api/skills/{self.lanzada.slug}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT, getattr(response, "data", None))
        self.assertFalse(Skill.objects.filter(slug="lanzada").exists())
        for fila in (self.org, self.plantilla):
            fila.refresh_from_db()
            self.assertEqual(_launches(fila.config, "borrador"), [])
            self.assertEqual(_launches(fila.config, "revision"), ["informe"])
        # La consola vuelve a poder guardar la configuración tal como quedó.
        _, errores = institution_config.validate_config(self.org.config, self.org)
        self.assertEqual(errores, [])
        evento = AuditEvent.objects.get(action="skill.removed_from_configs")
        self.assertEqual(evento.target_id, "lanzada")
        self.assertEqual(len(evento.data["configs"]), 2)

    def test_no_se_elimina_el_informe_de_una_etapa(self):
        response = self.client.delete(f"/api/skills/{self.informe.slug}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("informe de la etapa «revision»", str(response.data))
        self.assertTrue(Skill.objects.filter(slug="informe").exists())
        self.org.refresh_from_db()
        self.assertEqual(_launches(self.org.config, "revision"), ["lanzada", "informe"])

    def test_no_se_elimina_un_rol_de_workflows(self):
        self.org.config["workflows"]["guided"] = self.lanzada.slug
        self.org.save()
        response = self.client.delete(f"/api/skills/{self.lanzada.slug}/")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("workflows.guided", str(response.data))
        self.assertTrue(Skill.objects.filter(slug="lanzada").exists())
