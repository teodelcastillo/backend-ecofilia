"""
Tests for Sprint 3: incremental step output.

Verifies that each copilot step is persisted to the DB immediately after it
completes, so the frontend can poll and see progress before the full run ends.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import call, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.chat.tests.retrieval_mock import RetrievalMock
from apps.document.models import Document
from apps.project.models import Project, ProjectDocument
from apps.skill.models import (
    ExecutionOutputMode,
    Skill,
    SkillExecution,
    SkillStep,
    SkillType,
)
from apps.skill.services import execute_skill

User = get_user_model()


_chunk_id_counter = 0

def _make_chunk(doc, index=0):
    global _chunk_id_counter
    _chunk_id_counter += 1
    return SimpleNamespace(id=_chunk_id_counter, document=doc, chunk_index=index, content="chunk")


class IncrementalStepOutputTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="incr@example.com", password="secret123", username="incr"
        )
        self.project = Project.objects.create(owner=self.user, name="IncrProject")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-incr")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="Incremental Copilot",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="Be concise.",
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step One", instructions="First task.", position=1
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step Two", instructions="Second task.", position=2
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step Three", instructions="Third task.", position=3
        )

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.retrieve_for_chat", new_callable=RetrievalMock)
    def test_steps_completed_increments_per_step(self, mock_fetch, mock_completion):
        """steps_completed in DB must increase by 1 after each step, not only at the end."""
        mock_fetch.return_value = [_make_chunk(self.doc)]
        steps_completed_snapshots: list[int] = []

        original_completion = mock_completion

        call_index = [0]
        outputs = ["Output 1", "Output 2", "Output 3"]

        def track_completion(messages, **kwargs):
            result = (outputs[call_index[0]], {"total_tokens": 5})
            call_index[0] += 1
            # Read the DB state right after this call returns — service.py will
            # save before returning control here on the next save() after this call.
            return result

        mock_completion.side_effect = track_completion

        # Patch save to capture steps_completed snapshots after each step save.
        original_save = SkillExecution.save

        def patched_save(instance, *args, update_fields=None, **kwargs):
            original_save(instance, *args, update_fields=update_fields, **kwargs)
            if update_fields and "steps_completed" in update_fields:
                steps_completed_snapshots.append(instance.steps_completed)

        with patch.object(SkillExecution, "save", patched_save):
            execution = SkillExecution.objects.create(
                skill=self.skill, owner=self.user, project=self.project
            )
            execute_skill(execution)

        # Should have been saved incrementally: [1, 2, 3]
        self.assertEqual(steps_completed_snapshots, [1, 2, 3])

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.retrieve_for_chat", new_callable=RetrievalMock)
    def test_output_structured_grows_per_step(self, mock_fetch, mock_completion):
        """output_structured in DB must contain exactly N steps after N completions."""
        mock_fetch.return_value = [_make_chunk(self.doc)]
        output_structured_snapshots: list[int] = []

        call_count = [0]

        def track_completion(messages, **kwargs):
            call_count[0] += 1
            return (f"Output {call_count[0]}", {"total_tokens": 5})

        mock_completion.side_effect = track_completion

        original_save = SkillExecution.save

        def patched_save(instance, *args, update_fields=None, **kwargs):
            original_save(instance, *args, update_fields=update_fields, **kwargs)
            if update_fields and "output_structured" in update_fields:
                steps = instance.output_structured.get("steps", [])
                output_structured_snapshots.append(len(steps))

        with patch.object(SkillExecution, "save", patched_save):
            execution = SkillExecution.objects.create(
                skill=self.skill, owner=self.user, project=self.project
            )
            execute_skill(execution)

        # Steps available after each incremental save: [1, 2, 3]
        self.assertEqual(output_structured_snapshots, [1, 2, 3])

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.retrieve_for_chat", new_callable=RetrievalMock)
    def test_final_state_after_full_run(self, mock_fetch, mock_completion):
        """After full run, steps_completed equals total steps and output is complete."""
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.side_effect = [
            ("Out 1", {"total_tokens": 5}),
            ("Out 2", {"total_tokens": 5}),
            ("Out 3", {"total_tokens": 5}),
        ]

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.steps_completed, 3)
        self.assertEqual(len(execution.output_structured["steps"]), 3)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.retrieve_for_chat", new_callable=RetrievalMock)
    def test_steps_completed_persisted_even_on_failure(self, mock_fetch, mock_completion):
        """If the run fails mid-way, the steps that completed should still be in output_structured."""
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.side_effect = [
            ("Step 1 output", {"total_tokens": 5}),
            RuntimeError("LLM timeout"),
        ]

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "failed")
        # Step 1 output should still be persisted
        self.assertEqual(execution.steps_completed, 1)
        self.assertEqual(len(execution.output_structured.get("steps", [])), 1)
        self.assertEqual(execution.output_structured["steps"][0]["content"], "Step 1 output")

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.retrieve_for_chat", new_callable=RetrievalMock)
    def test_quick_skill_steps_completed_stays_zero(self, mock_fetch, mock_completion):
        """Quick skills don't use steps_completed — it should remain 0."""
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Result", {"total_tokens": 5})

        quick_skill = Skill.objects.create(
            owner=self.user,
            name="Quick",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            prompt_template="{{context}}",
        )
        execution = SkillExecution.objects.create(
            skill=quick_skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.steps_completed, 0)
