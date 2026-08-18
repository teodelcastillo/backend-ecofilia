from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument
from apps.skill.models import (
    ExecutionOutputMode,
    Skill,
    SkillExecution,
    SkillStep,
    SkillType,
    OutputValidation,
)
from apps.skill.services import execute_skill


User = get_user_model()


class SkillRunnerServiceTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="secret123",
            username="owner",
        )
        self.project = Project.objects.create(owner=self.user, name="Project 1")
        self.doc_a = Document.objects.create(owner=self.user, name="Doc A", slug="doc-a")
        self.doc_b = Document.objects.create(owner=self.user, name="Doc B", slug="doc-b")
        ProjectDocument.objects.create(project=self.project, document=self.doc_a, added_by=self.user)
        ProjectDocument.objects.create(project=self.project, document=self.doc_b, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="Goal Comparison",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            system_prompt="Be precise.",
            prompt_template="Documents:\n{{context}}\n\n{{extra_instructions}}",
            comparative_mode_enabled=True,
            strict_missing_evidence=True,
            retrieval_strategy="global",
            k_per_doc=2,
            total_limit=10,
            max_per_doc_after_rerank=3,
        )

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_quick_comparative_mode_enforces_prompt_and_hybrid_strategy(
        self, mock_fetch_chunks, mock_completion
    ):
        chunk_a = SimpleNamespace(document=self.doc_a, chunk_index=0, content="Goal A")
        chunk_b = SimpleNamespace(document=self.doc_b, chunk_index=1, content="Goal B")
        mock_fetch_chunks.return_value = [chunk_a, chunk_b]
        mock_completion.return_value = ("Result", {"total_tokens": 42})

        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            extra_instructions="Compare targets by criterion.",
        )

        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.metadata["retrieval_strategy_used"], "hybrid_per_document")
        self.assertTrue(execution.metadata["comparative_mode_enabled"])
        self.assertEqual(execution.metadata["docs_total"], 2)
        self.assertEqual(execution.metadata["docs_covered"], 2)

        mock_fetch_chunks.assert_called_once()
        fetch_kwargs = mock_fetch_chunks.call_args.kwargs
        self.assertEqual(fetch_kwargs["retrieval_strategy"], "hybrid_per_document")
        self.assertEqual(fetch_kwargs["k_per_doc"], 2)
        self.assertEqual(fetch_kwargs["total_limit"], 10)
        self.assertEqual(fetch_kwargs["max_chunks_per_doc"], 3)

        self.assertTrue(mock_completion.called)
        rendered_prompt = mock_completion.call_args.args[0][1]["content"]
        self.assertIn("Present findings by document first", rendered_prompt)
        self.assertIn("Sin evidencia en fuentes provistas", rendered_prompt)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_quick_table_mode_uses_schema_and_normalizes_output(
        self, mock_fetch_chunks, mock_completion
    ):
        chunk_a = SimpleNamespace(document=self.doc_a, chunk_index=0, content="Doc A row")
        mock_fetch_chunks.return_value = [chunk_a]
        mock_completion.return_value = (
            '{"type":"table","rows":[{"titulo":"A","anio":"2025","cumple":"si","estado":"ok"}]}',
            {"total_tokens": 10},
        )
        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            output_mode=ExecutionOutputMode.TABLE,
            metadata={
                "table_schema": {
                    "columns": [
                        {"key": "titulo", "type": "text", "required": True, "prompt_hint": ""},
                        {"key": "anio", "type": "number", "required": True, "prompt_hint": ""},
                        {"key": "cumple", "type": "boolean", "required": True, "prompt_hint": ""},
                        {
                            "key": "estado",
                            "type": "enum",
                            "required": False,
                            "prompt_hint": "",
                            "allowed_values": ["ok", "pendiente"],
                        },
                    ]
                }
            },
        )

        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.output, "")
        self.assertEqual(execution.output_structured["type"], "table")
        self.assertEqual(execution.output_structured["rows"][0]["anio"], 2025)
        self.assertIs(execution.output_structured["rows"][0]["cumple"], True)


class CopilotTabularStepsTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="copilot@example.com",
            password="secret123",
            username="copilot",
        )
        self.project = Project.objects.create(owner=self.user, name="Copilot Project")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-copilot")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="Mixed Copilot",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="Be concise.",
            prompt_template="",
            comparative_mode_enabled=False,
            retrieval_strategy="global",
        )
        SkillStep.objects.create(
            skill=self.skill,
            title="Resumen",
            instructions="Resume el documento en un parrafo.",
            position=1,
        )
        SkillStep.objects.create(
            skill=self.skill,
            title="Matriz",
            instructions="Genera la matriz de cumplimiento.",
            position=2,
            output_mode=ExecutionOutputMode.TABLE,
            table_schema={
                "name": "Matriz",
                "description": "",
                "columns": [
                    {
                        "key": "criterio",
                        "label": "Criterio",
                        "type": "text",
                        "required": True,
                        "prompt_hint": "",
                        "allowed_values": [],
                    },
                    {
                        "key": "cumple",
                        "label": "Cumple",
                        "type": "boolean",
                        "required": True,
                        "prompt_hint": "",
                        "allowed_values": [],
                    },
                ],
            },
        )

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_copilot_runs_text_then_table_step(self, mock_fetch_chunks, mock_completion):
        chunk = SimpleNamespace(document=self.doc, chunk_index=0, content="Document chunk")
        mock_fetch_chunks.return_value = [chunk]

        mock_completion.side_effect = [
            ("Resumen breve.", {"total_tokens": 5}),
            (
                '{"type":"table","rows":[{"criterio":"Inclusion","cumple":"si"},'
                '{"criterio":"Mitigacion","cumple":"no"}]}',
                {"total_tokens": 8},
            ),
        ]

        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
        )

        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        steps = execution.output_structured.get("steps", [])
        self.assertEqual(len(steps), 2)

        text_step, table_step = steps[0], steps[1]
        self.assertEqual(text_step["output_mode"], "text")
        self.assertEqual(text_step["content"], "Resumen breve.")

        self.assertEqual(table_step["output_mode"], "table")
        self.assertIn("table", table_step)
        self.assertEqual(table_step["table"]["columns"], ["criterio", "cumple"])
        self.assertEqual(len(table_step["table"]["rows"]), 2)
        self.assertIs(table_step["table"]["rows"][0]["cumple"], True)
        self.assertIs(table_step["table"]["rows"][1]["cumple"], False)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_lenient_table_step_invalid_json_falls_back_to_text(
        self, mock_fetch_chunks, mock_completion
    ):
        """Un paso tolerante conserva la degradación a texto.

        Es el comportamiento que tenían todos los pasos antes de que la política
        fuera configurable, y sigue siendo el correcto para un workflow
        exploratorio: la corrida termina y el error queda en `table_error`.
        """
        paso = self.skill.steps.get(position=2)
        paso.output_validation = OutputValidation.LENIENT
        paso.save(update_fields=["output_validation"])

        chunk = SimpleNamespace(document=self.doc, chunk_index=0, content="Document chunk")
        mock_fetch_chunks.return_value = [chunk]
        mock_completion.side_effect = [
            ("Resumen breve.", {"total_tokens": 5}),
            ("Esto no es JSON valido", {"total_tokens": 4}),
        ]

        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
        )

        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        steps = execution.output_structured.get("steps", [])
        self.assertEqual(steps[1]["output_mode"], "text")
        self.assertIn("table_error", steps[1])
        self.assertEqual(steps[1]["content"], "Esto no es JSON valido")

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_strict_table_step_does_not_silently_become_text(
        self, mock_fetch_chunks, mock_completion
    ):
        """Un paso estricto reintenta y, si tampoco cumple, falla.

        Degradar a texto acá es lo que hacía que una determinación auditable
        terminara siendo prosa mientras la ejecución quedaba en 'completed'.
        El reintento le pasa al modelo el error concreto; dos intentos fallidos
        contra un contrato explícito son suficiente evidencia.
        """
        paso = self.skill.steps.get(position=2)
        paso.output_validation = OutputValidation.STRICT
        paso.save(update_fields=["output_validation"])

        chunk = SimpleNamespace(document=self.doc, chunk_index=0, content="Document chunk")
        mock_fetch_chunks.return_value = [chunk]
        mock_completion.side_effect = [
            ("Resumen breve.", {"total_tokens": 5}),
            ("Esto no es JSON valido", {"total_tokens": 4}),
            ("Sigue sin ser JSON", {"total_tokens": 4}),
        ]

        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
        )

        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "failed")
        # El paso anterior quedó persistido: fallar no tira el trabajo hecho.
        steps = execution.output_structured.get("steps", [])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["output_mode"], "text")
