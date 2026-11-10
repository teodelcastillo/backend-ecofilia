"""
El contexto de la operación en el chat de Investigar.

- Cada dato lleva su origen: [Operación], [Ejecutivo] o [Ecofilia].
- Lo del ejecutivo es la decisión; lo de Ecofilia, la sugerencia del informe
  (aunque el ejecutivo no lo vea).
- Se lee en cada mensaje: un cambio del ejecutivo llega al siguiente.
- Sólo para operaciones de una institución con criterios de evaluación.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.chat.api.views import _compose_messages
from apps.chat.models import ChatSession
from apps.chat.services.rag import RetrievalResult
from apps.project.models import Project
from apps.project.services.chat_context import operation_context, origins_cited
from apps.project.services.stages import IET_SLUG
from apps.skill.models import ExecutionStatus, Skill, SkillExecution
from apps.user.models import Organization
from apps.user.tests.fixtures import caf_config

User = get_user_model()

E3 = "\n".join([
    "# E.3. Recomendaciones",
    "## E.3.1. Recomendaciones para fortalecer el alineamiento con el Objetivo 1: Desarrollo Resiliente al Clima",
    "- Evaluar los riesgos físicos por sitio [[c:1]].",
    "## E.3.3. Recomendaciones para fortalecer los co-beneficios",
    "- Incorporar arbolado nativo.",
])
C1 = (
    "**1. Paneles solares**\n\n| Campo | Detalle |\n|---|---|\n"
    "| Componente y actividad de la operación | Componente 1 – Obras: paneles |\n"
    "| Monto (USD) | USD 1.200.000 |\n"
    "| Categoría (nivel 2) | M.8 Eficiencia energética |\n"
    "| Actividad elegible (nivel 4) | M.8.1.1 Medidas que reduzcan el consumo neto de energía |\n"
)


class OperationContextTestCase(TestCase):
    def setUp(self):
        self.caf = Organization.objects.create(name="CAF", slug="caf", config=caf_config())
        self.owner = User.objects.create_user(email="owner@caf.com", password="x", organization=self.caf)
        self.project = Project.objects.create(
            owner=self.owner,
            organization=self.caf,
            name="Agua potable rural",
            description="Ampliación de redes.",
            context_notes={
                "estado": "ido_cci",
                "pais": "Perú",
                "alineacion_paris": {"ct_a1": "Riesgo alto", "actualizado_por": "Ana"},
                "alineacion_fundamentos": {"ct_a1": "El APRC lo califica alto por sequía."},
                "consideraciones_adicionales": "Capacidad limitada del ejecutor.",
            },
        )
        SkillExecution.objects.create(
            skill=Skill.objects.get(slug=IET_SLUG), owner=self.owner, project=self.project,
            status=ExecutionStatus.COMPLETED, stage="ido_cci", finished_at=timezone.now(),
            output_structured={"steps": [
                {"step_id": 1, "title": "C.1. Evaluación preliminar de los co-beneficios", "content": C1},
                {"step_id": 2, "title": "E.3. Recomendaciones para fortalecer la Alineación", "content": E3},
                {"step_id": 3, "title": "S.1. Síntesis por criterio", "content": "", "table": {"rows": [
                    {"criterio": "CT A1 — Análisis de riesgos climáticos", "determinacion": "Riesgo bajo",
                     "fundamento": "El APRC no muestra amenazas altas."},
                ]}},
            ]},
        )

    def test_each_part_carries_its_origin(self):
        texto = operation_context(self.project)
        general, ejecutivo, ecofilia = (
            texto.split("[Operación] Datos generales")[1].split("[Ejecutivo]")[0],
            texto.split("[Ejecutivo] Lo que determinó")[1].split("[Ecofilia] Lo que sugiere")[0],
            texto.split("[Ecofilia] Lo que sugiere")[1],
        )
        self.assertIn("Agua potable rural", general)
        self.assertIn("País: Perú", general)
        # La mirada del ejecutivo no se mezcla con los datos generales.
        self.assertNotIn("Riesgo alto", general)
        self.assertIn("CT A1 — Análisis de riesgos climáticos: Riesgo alto.", ejecutivo)
        self.assertIn("El APRC lo califica alto por sequía.", ejecutivo)
        self.assertIn("Capacidad limitada del ejecutor.", ejecutivo)
        self.assertIn("todavía no armó su informe", ejecutivo)
        # Lo de Ecofilia: su conclusión, sus actividades de C.1 y sus recomendaciones, sin marcas de cita.
        self.assertIn("CT A1 — Análisis de riesgos climáticos: Riesgo bajo.", ecofilia)
        self.assertIn("M.8.1.1", ecofilia)
        self.assertIn("USD 1.200.000", ecofilia)
        self.assertIn("Evaluar los riesgos físicos por sitio.", ecofilia)
        self.assertNotIn("[[c:", ecofilia)
        self.assertIn("Recomendaciones para fortalecer los co-beneficios:", ecofilia)

    def test_it_tells_how_to_answer_by_origin(self):
        texto = operation_context(self.project)
        self.assertIn("[Ejecutivo] es la decisión", texto)
        self.assertIn("[Ecofilia] es el análisis de Ecofilia: sugiere, no decide", texto)

    def test_without_executive_assessment_it_says_it_is_pending(self):
        self.project.context_notes = {"estado": "ido_cci"}
        self.project.save(update_fields=["context_notes"])
        texto = operation_context(self.project)
        self.assertIn("Todavía no determinó la alineación", texto)
        self.assertIn("Todavía no confirmó qué actividades cuenta como financiamiento verde", texto)

    def test_an_operation_without_criteria_gets_nothing(self):
        publico = User.objects.create_user(email="publico@example.com", password="x")
        otra = Project.objects.create(owner=publico, name="Sin institución")
        self.assertEqual(operation_context(otra), "")
        self.assertEqual(operation_context(None), "")

    def test_the_chat_reads_it_fresh_on_every_message(self):
        session = ChatSession.objects.create(owner=self.owner, project=self.project, system_prompt="Base.")
        primero = _compose_messages(session, "¿Está alineada?", RetrievalResult())
        self.assertTrue(any("Riesgo alto" in m["content"] for m in primero))
        notes = dict(self.project.context_notes)
        notes["alineacion_paris"] = {"ct_a1": "Riesgo medio"}
        Project.objects.filter(pk=self.project.pk).update(context_notes=notes)
        session.refresh_from_db()
        segundo = _compose_messages(session, "¿Y ahora?", RetrievalResult())
        self.assertTrue(any("Riesgo medio" in m["content"] for m in segundo))

    def test_a_failure_never_breaks_the_chat(self):
        with patch("apps.project.services.chat_context._datos_generales", side_effect=RuntimeError("x")):
            self.assertEqual(operation_context(self.project), "")

    def test_the_cited_origins_are_recorded(self):
        self.assertEqual(
            origins_cited("Según el ejecutivo [Ejecutivo], y el análisis sugiere otra cosa [Ecofilia]."),
            ["[Ejecutivo]", "[Ecofilia]"],
        )
        self.assertEqual(origins_cited("Nada de la operación [#2]."), [])
