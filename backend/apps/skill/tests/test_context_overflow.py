"""
Un pedido que la API rechaza por largo.

El presupuesto documental se calcula antes de llamar con un estimador por
caracteres. Si se queda corto, la API responde ``prompt is too long: X tokens >
Y maximum`` y la corrida se perdía entera (pasó con una IET en producción, por
517 tokens). Después se achicaba la ventana en lo que sobró, pero si el
estimador se equivocó por mucho el plan no cambiaba y el reintento era el mismo
pedido (ejecución 436, 17/11/2026: 1.033.145 tokens dos veces seguidas). Ahora
el rechazo recalibra el estimador con el conteo real y el paso se rearma.
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

    def test_recalibrates_with_the_real_count(self):
        scale = context_budget.recalibrate_scale(1.0, sent=1_033_145, estimated=800_000)
        self.assertAlmostEqual(scale, 1_033_145 / 800_000 * context_budget.RECALIBRATION_CUSHION)

    def test_a_small_miss_still_leaves_a_cushion(self):
        scale = context_budget.recalibrate_scale(1.0, sent=1_000_517, estimated=1_100_000)
        self.assertAlmostEqual(scale, context_budget.RECALIBRATION_CUSHION)

    def test_an_absurd_ratio_is_capped(self):
        scale = context_budget.recalibrate_scale(1.0, sent=1_000_517, estimated=1_000)
        self.assertEqual(scale, context_budget.MAX_TOKEN_SCALE)

    def test_never_goes_below_the_previous_scale(self):
        scale = context_budget.recalibrate_scale(1.5, sent=1_100_000, estimated=1_000_000)
        self.assertAlmostEqual(scale, 1.5 * context_budget.RECALIBRATION_CUSHION)

    def test_scaled_plan_degrades_what_the_unscaled_one_kept(self):
        class Doc:
            def __init__(self, id, text):
                self.id, self.slug, self.name, self.extracted_text = id, f"d{id}", f"D{id}", text

        docs = [Doc(1, "a" * 230), Doc(2, "b" * 2300)]  # 100 y 1000 tokens estimados
        plain = context_budget.plan_context(
            docs, reserved_tokens=0, blueprint_id=1, max_document_tokens=1200
        )
        self.assertTrue(plain.complete)
        scaled = context_budget.plan_context(
            docs, reserved_tokens=0, blueprint_id=1, max_document_tokens=1200, token_scale=1.5
        )
        self.assertEqual([d.slug for d in scaled.degraded], ["d2"])
        self.assertEqual(scaled.diagnostics()["token_scale"], 1.5)


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

    def test_the_step_is_rebuilt_with_a_recalibrated_estimator(self):
        usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
        with patch("apps.skill.services._call_model", side_effect=[OVERFLOW, ("Listo.", usage, "m")]) as call:
            execution = self._run()
        self.assertEqual(execution.status, "completed", execution.error_message)
        self.assertEqual(call.call_count, 2)
        step = execution.output_structured["steps"][0]
        self.assertEqual(step["content"], "Listo.")
        retry = step["retrieval"]["context_retry"]
        self.assertEqual(retry["sent_tokens"], 1000517)
        self.assertGreaterEqual(retry["token_scale"], context_budget.RECALIBRATION_CUSHION)

    def test_a_third_rejection_fails_the_run(self):
        with patch(
            "apps.skill.services._call_model", side_effect=[OVERFLOW, OVERFLOW, OVERFLOW]
        ) as call:
            execution = self._run()
        self.assertEqual(execution.status, "failed")
        self.assertEqual(call.call_count, 3)

    def test_a_large_miss_changes_the_request(self):
        """Ejecución 436: el reintento tiene que mandar otra cosa, no lo mismo."""
        annex = Document.objects.create(
            owner=self.user, name="BUR", slug="bur", chunking_status="done",
            extracted_text="12,3 | 45,6 | 78,9\n" * 300,
        )
        ProjectDocument.objects.create(project=self.project, document=annex)
        self.skill.max_document_tokens = 3_000
        self.skill.save(update_fields=["max_document_tokens"])

        sent: list[list[dict]] = []
        usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}

        def fake_call(messages, **kwargs):
            sent.append(messages)
            if len(sent) == 1:
                real = 3 * context_budget.estimate_messages_tokens(messages)
                raise Exception(
                    f"Error code: 400 - prompt is too long: {real} tokens > 1000000 maximum"
                )
            return "Listo.", usage, "m"

        with patch("apps.skill.services._call_model", side_effect=fake_call), patch(
            "apps.skill.services.retrieve_for_chat", side_effect=Exception("sin índice")
        ):
            execution = self._run()

        self.assertEqual(execution.status, "completed", execution.error_message)
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0], sent[1])
        retrieval = execution.output_structured["steps"][0]["retrieval"]
        self.assertEqual(retrieval["documents_partial"], ["bur"])
        self.assertGreater(retrieval["token_scale"], 2.9)

    def test_other_errors_are_not_retried(self):
        with patch("apps.skill.services._call_model", side_effect=Exception("credit balance is too low")) as call:
            execution = self._run()
        self.assertEqual(execution.status, "failed")
        self.assertEqual(call.call_count, 1)
