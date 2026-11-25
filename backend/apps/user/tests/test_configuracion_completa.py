"""
Todo lo de una institución se configura desde la consola.

Lo que antes estaba escrito para CAF en el código y ahora es configuración:
- el estilo del portal y la plantilla del informe;
- las secciones del portal y los temas fijados de su biblioteca;
- las etapas en preparación (el bloqueo de DEC);
- el formulario de la operación (la carátula), y con él el contexto que
  reciben los asistentes y qué completa la IA;
- las copias de skills de Ecofilia para una institución que ya existe.

Contrato en ``backend/docs/contrato-instituciones.md``, «Configuración completa
desde la consola».
"""
import copy
import importlib
from types import SimpleNamespace

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.project.services.ai_fill import EXTRACTABLE_FIELDS, TIPOS_OPERACION, institution_fields
from apps.skill.models import Skill, SkillScope, SkillStep, SkillType
from apps.skill.operation_context import build_operation_context_block
from apps.user import institution_config, operation_form
from apps.user.models import Organization
from apps.user.onboarding import RESERVED_SLUGS, _remap
from apps.user.tests.fixtures import caf_config, make_superadmin

User = get_user_model()
COMPLETA = importlib.import_module("apps.user.migrations.0018_configuracion_completa_caf")


def _plantilla(**cambios) -> dict:
    plantilla = {
        "title": "INFORME DE EVALUACIÓN",
        "subtitle": "Banco del Sur",
        "file_prefix": "Informe",
        "assistant_name": "el asistente",
        "letterhead": {"logo_url": "/images/logo.png", "logo_width": 180, "logo_height": 31, "lines": ["Banco"]},
        "cover": {"intro": "", "structure_lead": "", "structure": [], "disclaimer": ""},
        "sections": [
            {"key": "A", "heading": "SECCIÓN A", "source": "operacion"},
            {"key": "*", "heading": "ANÁLISIS", "source": "analisis"},
        ],
        "style": {
            "body_font": "Aptos", "display_font": "Aptos Display", "text_color": "000000",
            "heading_color": "2e74b5", "subheading_color": "005288", "accent_color": "13B67E",
            "table_header_color": "E7F5EF", "table_border_color": "D8E5E0",
        },
    }
    plantilla.update(cambios)
    return plantilla


def _validar(cambios: dict, organization=None):
    config = copy.deepcopy(institution_config.BASE_CONFIG)
    config.update(cambios)
    return institution_config.validate_config(config, organization)


class EstiloYPlantillaTestCase(TestCase):
    def test_la_base_siempre_trae_la_plantilla_aunque_sea_null(self):
        self.assertIsNone(institution_config.effective_config(None)["report_template"])
        org = Organization.objects.create(name="Sur", slug="sur", config={"version": 1})
        self.assertIn("report_template", institution_config.effective_config(org))

    def test_estilo_del_portal_normalizado(self):
        config, errores = _validar(
            {"branding": {"display_name": "Sur", "logo_url": "", "primary_color": "1d4ed8", "portal_title": "  Portal  "}}
        )
        self.assertEqual(errores, [])
        self.assertEqual(config["branding"]["primary_color"], "1D4ED8")
        self.assertEqual(config["branding"]["portal_title"], "Portal")
        self.assertNotIn("portal_logo_url", config["branding"])

    def test_estilo_invalido(self):
        _, errores = _validar(
            {"branding": {"display_name": "Sur", "logo_url": "", "primary_color": "#123", "portal_logo_url": "ftp://x"}}
        )
        self.assertIn("branding.primary_color: un color hexadecimal de seis dígitos, sin «#».", errores)
        self.assertTrue(any(e.startswith("branding.portal_logo_url") for e in errores), errores)

    def test_plantilla_normalizada(self):
        config, errores = _validar({"report_template": _plantilla()})
        self.assertEqual(errores, [])
        plantilla = config["report_template"]
        self.assertEqual(plantilla["style"]["heading_color"], "2E74B5")
        self.assertEqual(plantilla["sections"][0]["include_objective"], True)
        self.assertEqual(plantilla["sections"][1]["conclusion"], None)

    def test_plantilla_con_errores_juntos(self):
        _, errores = _validar(
            {
                "report_template": _plantilla(
                    title="",
                    sections=[
                        {"key": "S", "heading": "x", "source": "analisis"},
                        {"key": "B", "heading": "B", "source": "operacion"},
                        {"key": "B", "heading": "B", "source": "operacion"},
                    ],
                )
            }
        )
        self.assertIn("report_template.title: no puede estar vacío.", errores)
        self.assertIn(
            "report_template.sections[0].key: «S» está reservada para los pasos internos del asistente.", errores
        )
        self.assertIn("report_template.sections[2].key: «B» está repetida.", errores)
        self.assertIn("report_template.sections: puede haber una sola sección con los datos de la operación.", errores)


class PortalYEtapasTestCase(TestCase):
    def test_secciones_en_el_orden_de_la_navegacion(self):
        config, errores = _validar({"portal": {"sections": ["tablero", "biblioteca", "tablero"]}})
        self.assertEqual(errores, [])
        self.assertEqual(config["portal"], {"sections": ["biblioteca", "tablero"], "pinned_topics": []})

    def test_seccion_desconocida_y_temas_repetidos(self):
        _, errores = _validar(
            {
                "portal": {
                    "sections": ["chat"],
                    "pinned_topics": [{"acronym": "NDC", "label": "NDC"}, {"acronym": "ndc", "label": "Otra"}],
                }
            }
        )
        self.assertTrue(any(e.startswith("portal.sections[0]") for e in errores), errores)
        self.assertIn("portal.pinned_topics[1].acronym: «ndc» está repetido.", errores)

    def test_etapa_en_preparacion(self):
        etapas = [
            {"slug": "inicio", "label": "Inicio", "principal_tag": None, "launches": []},
            {"slug": "dec", "label": "DEC", "principal_tag": None, "launches": [], "en_preparacion": True},
            {"slug": "fin", "label": "Fin", "principal_tag": None, "launches": [], "en_preparacion": False},
        ]
        config, errores = _validar({"stages": etapas})
        self.assertEqual(errores, [])
        self.assertTrue(config["stages"][1]["en_preparacion"])
        self.assertNotIn("en_preparacion", config["stages"][2])
        self.assertEqual(institution_config.stages_in_preparation(config), ["dec"])

    def test_la_primera_etapa_no_puede_estar_en_preparacion(self):
        _, errores = _validar(
            {"stages": [{"slug": "inicio", "label": "Inicio", "principal_tag": None, "launches": [], "en_preparacion": True}]}
        )
        self.assertIn("stages[0].en_preparacion: la primera etapa no puede estar en preparación.", errores)


class FormularioDeOperacionTestCase(TestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        Skill.objects.filter(slug__startswith="caf-").update(scope=SkillScope.ORGANIZATION, organization=self.caf)

    def test_la_de_caf_es_valida_y_queda_igual(self):
        config, errores = institution_config.validate_config(caf_config(), self.caf)
        self.assertEqual(errores, [])
        self.assertEqual(config["operation_form"], COMPLETA.OPERATION_FORM)

    def test_la_base_trae_un_formulario_sin_nada_de_caf(self):
        form = institution_config.effective_config(None)["operation_form"]
        claves = [f["key"] for f in operation_form.fields(form)]
        self.assertIn("pais", claves)
        self.assertNotIn("ejecutivo_datbc", claves)
        self.assertNotIn("CAF", str(form))

    def test_errores_de_campos(self):
        form = {
            "blocks": [
                {
                    "title": "Identidad",
                    "fields": [
                        {"key": "pais", "label": "País", "type": "select"},
                        {"key": "pais", "label": "Otro país"},
                        {"key": "estado", "label": "Etapa", "type": "text"},
                        {"key": "enverdecimiento", "label": "Verde"},
                        {"key": "sector", "label": "Sector", "type": "select", "options": ["A"], "ai": "suggest"},
                    ],
                },
                {
                    "title": "Más",
                    "stages": ["no-existe"],
                    "fields": [
                        {"key": "monto", "label": "Monto", "required": True, "unit": "USD", "type": "number"},
                        {"key": "eco", "label": "Eco", "type": "multiselect", "options": ["a"], "other_option": "Otros"},
                        {"key": "eco_otros", "label": "Pisa el texto de Otros"},
                    ],
                },
            ],
            "caratula": [{"key": "numero_operacion"}, {"key": "pais"}, {"key": "pais"}],
        }
        _, errores = _validar({"operation_form": form})
        esperados = [
            "operation_form.blocks[0].fields[0].options: tiene que ser una lista con al menos una opción.",
            "operation_form.blocks[0].fields[1].key: «pais» está repetida.",
            "operation_form.blocks[0].fields[2]: la etapa es el campo «estado», de tipo etapa, y ningún otro.",
            "operation_form.blocks[0].fields[3].key: «enverdecimiento» la usa el sistema; elegí otra.",
            "operation_form.blocks[0].fields[4].ai: la IA sabe completar «suggest» sólo en tipo_operacion.",
            "operation_form.blocks[1].stages[0]: «no-existe» no es una etapa de esta configuración.",
            "operation_form.blocks[1].fields[0].required: sólo los campos del primer bloque (la identidad) "
            "pueden ser obligatorios.",
            "operation_form.caratula[0].label: «numero_operacion» no es un campo del formulario; necesita un rótulo.",
            "operation_form.caratula[2].key: «pais» está repetida.",
        ]
        for esperado in esperados:
            self.assertIn(esperado, errores)
        self.assertTrue(any("«eco_otros» guarda el texto de «Otros»" in e for e in errores), errores)

    def test_la_identidad_se_muestra_siempre(self):
        _, errores = _validar(
            {"operation_form": {"blocks": [{"title": "Identidad", "only_edit": True, "fields": [{"key": "a", "label": "A"}]}]}}
        )
        self.assertIn(
            "operation_form.blocks[0]: la identidad se muestra siempre; no lleva etapas ni «sólo al editar».", errores
        )

    def test_los_modos_de_ia_son_los_que_sabe_el_extractor(self):
        for claves in operation_form.AI_KEYS.values():
            for clave in claves:
                self.assertIn(clave, EXTRACTABLE_FIELDS)


class MigracionCafTestCase(TestCase):
    def test_aplica_solo_a_caf_y_vuelve(self):
        base = importlib.import_module("apps.user.migrations.0009_config_caf").CAF_CONFIG
        COMPLETA.aplicar(django_apps, None)  # sin CAF: no hace nada
        caf = Organization.objects.create(name="CAF", slug="caf", config=copy.deepcopy(base))
        otra = Organization.objects.create(name="Otra", slug="otra")
        COMPLETA.aplicar(django_apps, None)
        caf.refresh_from_db()
        otra.refresh_from_db()
        self.assertEqual(caf.config["operation_form"], COMPLETA.OPERATION_FORM)
        self.assertIn("proyeccion", caf.config["portal"]["sections"])
        self.assertEqual(institution_config.stages_in_preparation(caf.config), ["dec"])
        self.assertEqual(otra.config, {})
        COMPLETA.revertir(django_apps, None)
        caf.refresh_from_db()
        self.assertEqual(caf.config, base)


class ContextoDeLosAsistentesTestCase(TestCase):
    """Los rótulos y los datos que van al análisis salen del formulario."""

    def _bloque(self, notes, config):
        project = SimpleNamespace(name="Op", description="", blueprint_document=None, context_notes=notes)
        return build_operation_context_block(project, config=config)

    def test_en_caf_queda_como_antes(self):
        config = caf_config()
        notes = {
            "pais": "Perú", "monto": "40", "estado": "ido_cci", "ejecutivo_datbc": "Nara Vargas",
            "link_focus": "https://focus", "cliente": "MEF", "numero_operacion": "123",
        }
        bloque = self._bloque(notes, config)
        self.assertIn("- País: Perú", bloque)
        self.assertIn("- Monto CAF (millones de USD): 40", bloque)
        self.assertIn("- Estado de avance: IDO-CCI", bloque)
        self.assertIn("- Número de operación: 123", bloque)
        self.assertIn("- Cliente: MEF", bloque)
        self.assertNotIn("Nara Vargas", bloque)
        self.assertNotIn("focus", bloque)

    def test_otra_institucion_usa_sus_rotulos_y_su_criterio(self):
        config = copy.deepcopy(institution_config.BASE_CONFIG)
        config["stages"] = [{"slug": "borrador", "label": "Borrador", "principal_tag": None, "launches": []}]
        config["operation_form"]["blocks"][1]["fields"].append(
            {**COMPLETA._campo("consecutivo", "Número interno")}
        )
        bloque = self._bloque({"monto": "10", "consecutivo": "X-1", "estado": "borrador"}, config)
        self.assertIn("- Monto (millones de USD): 10", bloque)
        self.assertIn("- Número interno: X-1", bloque)
        self.assertIn("- Estado de avance: Borrador", bloque)
        self.assertNotIn("CAF", bloque)


class IAPorInstitucionTestCase(TestCase):
    def _op(self, organization):
        from apps.project.models import Project

        owner = User.objects.create_user(email=f"o-{organization.slug if organization else 'x'}@x.org", password="x")
        return Project.objects.create(name="Op", owner=owner, organization=organization)

    def test_caf_completa_objetivo_componentes_y_sugiere_el_tipo_de_su_lista(self):
        caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        campos = institution_fields(self._op(caf))
        self.assertEqual(sorted(campos), ["componentes", "objetivo", "tipo_operacion"])
        self.assertEqual(campos["tipo_operacion"].choices, TIPOS_OPERACION)

    def test_sin_formulario_propio_no_recibe_los_tipos_de_caf(self):
        org = Organization.objects.create(name="Sur", slug="sur")
        self.assertEqual(sorted(institution_fields(self._op(org))), ["componentes", "objetivo"])

    def test_con_sus_propios_tipos(self):
        config = copy.deepcopy(institution_config.BASE_CONFIG)
        config["operation_form"]["blocks"][1]["fields"][0] = COMPLETA._campo(
            "tipo_operacion", "Tipo", "select", options=["Crédito", "Garantía"], ai="suggest"
        )
        org = Organization.objects.create(name="Sur", slug="sur", config=config)
        campo = institution_fields(self._op(org))["tipo_operacion"]
        self.assertEqual(campo.choices, ("Crédito", "Garantía"))
        self.assertIn("«Garantía»", campo.description)
        self.assertNotIn("PPI", campo.description)


class CopiasDeSkillsTestCase(APITestCase):
    def setUp(self):
        self.sur = Organization.objects.create(name="Sur", slug="sur")
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        self.workflow = Skill.objects.create(
            name="Informe", slug="informe-ecofilia", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"], scope=SkillScope.PLATFORM,
        )
        SkillStep.objects.create(skill=self.workflow, title="1. Contexto", instructions="Describí", position=1)
        self.de_caf = Skill.objects.create(
            name="De CAF", slug="caf-propia", skill_type=SkillType.COPILOT, allowed_contexts=["project"],
            scope=SkillScope.ORGANIZATION, organization=self.caf,
        )
        self.url = "/api/admin/organizations/sur/skills/"

    def test_superadmin_copia_una_de_ecofilia(self):
        self.client.force_authenticate(make_superadmin())
        response = self.client.post(self.url, {"clone": ["informe-ecofilia"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["cloned_skills"], [{"from": "informe-ecofilia", "slug": "sur-informe-ecofilia"}])
        copia = Skill.objects.get(slug="sur-informe-ecofilia")
        self.assertEqual((copia.scope, copia.organization), (SkillScope.ORGANIZATION, self.sur))
        self.assertEqual(copia.steps.count(), 1)
        self.assertIn("sur-informe-ecofilia", response.data["enabled_skill_slugs"])
        self.assertIn("sur-informe-ecofilia", response.data["default_skill_slugs"])

    def test_no_copia_de_otra_institucion_ni_vacio(self):
        self.client.force_authenticate(make_superadmin())
        response = self.client.post(self.url, {"clone": ["caf-propia"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Skill.objects.filter(organization=self.sur).exists())
        response = self.client.post(self.url, {"clone": []}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_solo_superadmin(self):
        miembro = User.objects.create_user(email="m@sur.org", password="x", organization=self.sur, role="admin")
        self.client.force_authenticate(miembro)
        response = self.client.post(self.url, {"clone": ["informe-ecofilia"]}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class AltaTestCase(TestCase):
    def test_slugs_reservados_del_front(self):
        for slug in ("skills", "tableros", "repositories", "nueva", "settings"):
            self.assertIn(slug, RESERVED_SLUGS)

    def test_la_plantilla_apunta_a_la_copia_tambien_en_el_informe_de_la_etapa(self):
        config = {"stages": [{"slug": "a", "launches": ["x"], "report": "x"}], "workflows": {"report": "x"}}
        remapeada = _remap(config, {"x": "sur-x"})
        self.assertEqual(remapeada["stages"][0]["report"], "sur-x")
        self.assertEqual(remapeada["stages"][0]["launches"], ["sur-x"])
        self.assertEqual(remapeada["workflows"]["report"], "sur-x")


class BorrarEtiquetaTestCase(APITestCase):
    """Una etiqueta que nombra la configuración de una institución no se borra."""

    def test_primero_se_saca_de_la_configuracion(self):
        from apps.document.models import EvidenceTag

        org = Organization.objects.create(name="Sur", slug="sur")
        EvidenceTag.objects.create(name="Propia", slug="propia", organization=org)
        config = copy.deepcopy(institution_config.BASE_CONFIG)
        config["evidence_tag_groups"] = [{"slug": "g", "label": "Grupo", "tags": ["propia"]}]
        org.config = config
        org.save()
        self.client.force_authenticate(make_superadmin())
        response = self.client.delete("/api/document/evidence-tags/propia/?organization=sur")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("grupo «Grupo»", str(response.data))
        org.config = {}
        org.save()
        response = self.client.delete("/api/document/evidence-tags/propia/?organization=sur")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(EvidenceTag.objects.filter(slug="propia").exists())
