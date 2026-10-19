"""
Un pedido que la API rechaza por largo.

El presupuesto documental se calcula antes de llamar con un estimador por
caracteres. Si se queda corto, la API responde ``prompt is too long: X tokens >
Y maximum`` y la corrida se perdía entera (pasó con una IET en producción, por
517 tokens). Ahora el paso se arma de nuevo una vez, con la ventana achicada en
lo que sobró más un colchón, y queda anotado.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument
from apps.skill import context_budget
from apps.skill.models import Skill, SkillExecution, SkillStep, SkillType
from apps.skill.services import execute_skill

User = get_user_model()
OVERFLOW = Exception(
    "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
    "'message': 'prompt is too long: 1000517 tokens > 1000000 maximum'}}"
)


class OverflowParsingTests(SimpleTestCase):
    def test_reads_the_api_rejection(self):
        self.assertEqual(context_budget.context_overflow(OVERFLOW), (1000517, 1000000))
        self.assertIsNone(context_budget.context_overflow(Exception("credit balance is too low")))

    def test_shrinks_by_the_excess_and_a_cushion(self):
        self.assertEqual(
            context_budget.shrink_window(1_000_000, 1_000_517, 1_000_000),
            1_000_000 - 517 - context_budget.CONTEXT_OVERFLOW_EXTRA,
        )


class OverflowRetryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(email="o@example.com", password="x", username="o")
        self.project = Project.objects.create(owner=self.user, name="Operación")
        self.doc = Document.objects.create(
            owner=self.user, name="IDO", slug="ido", chunking_status="done",
            extracted_text="La operación financia agua potable rural.",
        )
        ProjectDocument.objects.create(project=self.project, document=self.doc, is_primary=True)
        self.project.blueprint_document = self.doc
        self.project.save(update_fields=["blueprint_document"])
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"], system_prompt="", prompt_template="",
        )
        SkillStep.objects.create(skill=self.skill, title="B.1. Marco", instructions="Describí.", position=1)

    def _run(self):
        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project,
        )
        execute_skill(execution)
        execution.refresh_from_db()
        return execution

    def test_the_step_is_rebuilt_once_with_a_smaller_window(self):
        usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        with patch("apps.skill.services._call_model", side_effect=[OVERFLOW, ("Listo.", usage, "m")]) as call:
            execution = self._run()
        self.assertEqual(execution.status, "completed", execution.error_message)
        self.assertEqual(call.call_count, 2)
        step = execution.output_structured["steps"][0]
        self.assertEqual(step["content"], "Listo.")
        self.assertEqual(step["retrieval"]["context_retry"]["sent_tokens"], 1000517)

    def test_a_second_rejection_fails_the_run(self):
        with patch("apps.skill.services._call_model", side_effect=[OVERFLOW, OVERFLOW]) as call:
            execution = self._run()
        self.assertEqual(execution.status, "failed")
        self.assertEqual(call.call_count, 2)

    def test_other_errors_are_not_retried(self):
        with patch("apps.skill.services._call_model", side_effect=Exception("credit balance is too low")) as call:
            execution = self._run()
        self.assertEqual(execution.status, "failed")
        self.assertEqual(call.call_count, 1)
