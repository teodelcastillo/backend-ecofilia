"""
Tests for Sprint 4: human-in-the-loop.

Covers:
- Execution pauses at a step marked approval_required=True.
- Status transitions to AWAITING_APPROVAL.
- approve_step() / regenerate_step() service functions.
- Resume resumes from the correct step and preserves prior context.
- Override content is used by subsequent steps.
- API endpoints /approve/ and /regenerate-step/.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument
from apps.skill.models import (
    ExecutionStatus,
    Skill,
    SkillExecution,
    SkillStep,
    SkillType,
)
from apps.skill.services import approve_step, execute_skill, regenerate_step

User = get_user_model()


_chunk_id_counter = 0

def _make_chunk(doc, index=0):
    global _chunk_id_counter
    _chunk_id_counter += 1
    return SimpleNamespace(id=_chunk_id_counter, document=doc, chunk_index=index, content="chunk")


class StepApprovalGateTestCase(TestCase):
    """Runner pauses when it hits a step with approval_required=True."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="gate@example.com", password="secret123", username="gate", role="admin")
        self.project = Project.objects.create(owner=self.user, name="GateProject")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-gate")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="Gated Copilot",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="ESG analyst.",
        )
        SkillStep.objects.create(
            skill=self.skill, title="Scope", instructions="Define scope.", position=1,
            approval_required=True,   # ← gate here
        )
        SkillStep.objects.create(
            skill=self.skill, title="Risks", instructions="List risks.", position=2
        )

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_pauses_at_approval_gate(self, mock_fetch, mock_completion):
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Scope output.", {"total_tokens": 5})

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, ExecutionStatus.AWAITING_APPROVAL)
        self.assertEqual(execution.current_step_position, 1)
        # Only the first step should be in the output
        steps = execution.output_structured.get("steps", [])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["title"], "Scope")

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_only_gated_step_runs_before_pause(self, mock_fetch, mock_completion):
        """The LLM should be called once (for step 1) before pausing — not for step 2."""
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Scope output.", {"total_tokens": 5})

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)

        self.assertEqual(mock_completion.call_count, 1)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_no_gate_completes_fully(self, mock_fetch, mock_completion):
        """Without approval gates, execution should complete normally."""
        # Remove the gate
        SkillStep.objects.filter(skill=self.skill, position=1).update(approval_required=False)
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.side_effect = [
            ("Scope done.", {"total_tokens": 5}),
            ("Risks done.", {"total_tokens": 5}),
        ]

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, ExecutionStatus.COMPLETED)
        self.assertIsNone(execution.current_step_position)


class ReviewEachStepTestCase(TestCase):
    """
    Block-by-block confirmation: review_each_step=True forces a pause after
    every step except the last, regardless of each step's approval_required.
    This is the fix for "approving one step confirms all the rest".
    """

    def setUp(self):
        self.user = User.objects.create_user(
            email="review@example.com", password="secret123", username="review", role="admin")
        self.project = Project.objects.create(owner=self.user, name="ReviewProject")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-review")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="Review Copilot",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="ESG.",
        )
        # Three steps, none marked approval_required.
        for pos, title in [(1, "Scope"), (2, "Risks"), (3, "Summary")]:
            SkillStep.objects.create(
                skill=self.skill, title=title, instructions=f"{title}.", position=pos,
            )

    def _make_execution(self):
        return SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            metadata={"review_each_step": True},
        )

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_pauses_after_first_step_without_approval_flag(self, mock_fetch, mock_completion):
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Scope output.", {"total_tokens": 5})

        execution = self._make_execution()
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, ExecutionStatus.AWAITING_APPROVAL)
        self.assertEqual(execution.current_step_position, 1)
        self.assertEqual(len(execution.output_structured.get("steps", [])), 1)
        self.assertEqual(mock_completion.call_count, 1)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_pauses_at_each_step_then_completes_on_last(self, mock_fetch, mock_completion):
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("step output", {"total_tokens": 5})

        execution = self._make_execution()

        # Step 1 → pause
        execute_skill(execution)
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.AWAITING_APPROVAL)
        self.assertEqual(execution.current_step_position, 1)

        # Approve → step 2 → pause again (not the last step)
        approve_step(execution)
        execute_skill(execution)
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.AWAITING_APPROVAL)
        self.assertEqual(execution.current_step_position, 2)

        # Approve → step 3 (last) → completes without pausing
        approve_step(execution)
        execute_skill(execution)
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.COMPLETED)
        self.assertIsNone(execution.current_step_position)
        self.assertEqual(len(execution.output_structured["steps"]), 3)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_per_step_and_global_sources_populated(self, mock_fetch, mock_completion):
        mock_fetch.return_value = [_make_chunk(self.doc, index=2)]
        mock_completion.return_value = ("output", {"total_tokens": 5})

        execution = self._make_execution()
        execute_skill(execution)
        execution.refresh_from_db()

        step = execution.output_structured["steps"][0]
        self.assertTrue(step.get("sources"))
        self.assertEqual(step["sources"][0]["document_slug"], "doc-review")
        self.assertEqual(step["sources"][0]["chunk_index"], 2)


class ApproveStepServiceTestCase(TestCase):
    """approve_step() state transitions and override behaviour."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="approve@example.com", password="secret123", username="approve", role="admin")
        self.project = Project.objects.create(owner=self.user, name="ApproveProject")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-approve")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="Approve Copilot",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="ESG.",
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step 1", instructions="First.", position=1,
            approval_required=True,
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step 2", instructions="Second.", position=2,
        )

    def _execution_awaiting(self):
        """Create an execution already in AWAITING_APPROVAL state."""
        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.AWAITING_APPROVAL,
            output_structured={
                "steps": [{"step_id": 1, "title": "Step 1", "content": "Original.", "output_mode": "text"}]
            },
            steps_completed=1,
            current_step_position=1,
        )
        return execution

    def test_approve_sets_status_to_pending(self):
        execution = self._execution_awaiting()
        result = approve_step(execution)
        self.assertEqual(result.status, ExecutionStatus.PENDING)

    def test_approve_clears_error_message(self):
        execution = self._execution_awaiting()
        execution.error_message = "old error"
        execution.save()
        result = approve_step(execution)
        self.assertEqual(result.error_message, "")

    def test_approve_with_override_replaces_content(self):
        execution = self._execution_awaiting()
        result = approve_step(execution, override_content="Edited by consultant.")
        steps = result.output_structured["steps"]
        self.assertEqual(steps[0]["content"], "Edited by consultant.")
        self.assertTrue(steps[0].get("human_edited"))

    def test_approve_without_override_keeps_content(self):
        execution = self._execution_awaiting()
        result = approve_step(execution)
        steps = result.output_structured["steps"]
        self.assertEqual(steps[0]["content"], "Original.")

    def test_approve_fails_when_not_awaiting(self):
        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.COMPLETED,
        )
        with self.assertRaises(ValueError):
            approve_step(execution)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_resume_after_approve_runs_remaining_steps(self, mock_fetch, mock_completion):
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Step 2 output.", {"total_tokens": 5})

        execution = self._execution_awaiting()
        approve_step(execution)
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, ExecutionStatus.COMPLETED)
        self.assertEqual(execution.steps_completed, 2)
        self.assertEqual(len(execution.output_structured["steps"]), 2)
        # Step 1 is preserved, step 2 is new
        self.assertEqual(execution.output_structured["steps"][0]["content"], "Original.")
        self.assertEqual(execution.output_structured["steps"][1]["content"], "Step 2 output.")

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_override_content_flows_into_next_step_prompt(self, mock_fetch, mock_completion):
        """When the consultant edits step 1, step 2's prompt should include the edited text."""
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Step 2 output.", {"total_tokens": 5})

        execution = self._execution_awaiting()
        approve_step(execution, override_content="Consultant's improved scope.")
        execute_skill(execution)

        # The prompt for step 2 should include the override
        step2_prompt = mock_completion.call_args.args[0][1]["content"]
        self.assertIn("Consultant's improved scope.", step2_prompt)


class RegenerateStepServiceTestCase(TestCase):
    """regenerate_step() discards the last step and re-runs it."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="regen@example.com", password="secret123", username="regen", role="admin")
        self.project = Project.objects.create(owner=self.user, name="RegenProject")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-regen")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="Regen Copilot",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="ESG.",
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step 1", instructions="First.", position=1,
            approval_required=True,
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step 2", instructions="Second.", position=2,
        )

    def _execution_awaiting(self):
        return SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.AWAITING_APPROVAL,
            output_structured={
                "steps": [{"step_id": 1, "title": "Step 1", "content": "Bad output.", "output_mode": "text"}]
            },
            steps_completed=1,
            current_step_position=1,
        )

    def test_regenerate_sets_status_to_pending(self):
        execution = self._execution_awaiting()
        result = regenerate_step(execution)
        self.assertEqual(result.status, ExecutionStatus.PENDING)

    def test_regenerate_removes_last_step(self):
        execution = self._execution_awaiting()
        result = regenerate_step(execution)
        self.assertEqual(result.output_structured.get("steps", []), [])
        self.assertEqual(result.steps_completed, 0)

    def test_regenerate_clears_current_step_position(self):
        execution = self._execution_awaiting()
        result = regenerate_step(execution)
        self.assertIsNone(result.current_step_position)

    def test_regenerate_fails_when_not_awaiting(self):
        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.RUNNING,
        )
        with self.assertRaises(ValueError):
            regenerate_step(execution)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_regenerate_then_execute_reruns_step(self, mock_fetch, mock_completion):
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Improved step 1.", {"total_tokens": 5})

        execution = self._execution_awaiting()
        regenerate_step(execution)
        execute_skill(execution)
        execution.refresh_from_db()

        # Should pause again because step 1 has approval_required
        self.assertEqual(execution.status, ExecutionStatus.AWAITING_APPROVAL)
        steps = execution.output_structured.get("steps", [])
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["content"], "Improved step 1.")


class HumanInTheLoopAPITestCase(TestCase):
    """API endpoints for /approve/ and /regenerate-step/."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="api@example.com", password="secret123", username="apiuser", role="admin")
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(owner=self.user, name="APIProject")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-api")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="API Copilot",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="ESG.",
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step 1", instructions=".", position=1, approval_required=True
        )
        SkillStep.objects.create(
            skill=self.skill, title="Step 2", instructions=".", position=2
        )

    def _awaiting_execution(self):
        return SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.AWAITING_APPROVAL,
            output_structured={
                "steps": [{"step_id": 1, "title": "Step 1", "content": "Draft.", "output_mode": "text"}]
            },
            steps_completed=1,
            current_step_position=1,
        )

    @patch("apps.skill.dispatch.run_skill_task")
    def test_approve_returns_202(self, mock_task):
        mock_task.delay = mock_task
        execution = self._awaiting_execution()
        response = self.client.post(f"/api/skills/executions/{execution.id}/approve/", {})
        self.assertEqual(response.status_code, 202)
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.PENDING)

    @patch("apps.skill.dispatch.run_skill_task")
    def test_approve_with_override(self, mock_task):
        mock_task.delay = mock_task
        execution = self._awaiting_execution()
        response = self.client.post(
            f"/api/skills/executions/{execution.id}/approve/",
            {"override_content": "Edited."},
        )
        self.assertEqual(response.status_code, 202)
        execution.refresh_from_db()
        self.assertEqual(execution.output_structured["steps"][0]["content"], "Edited.")

    def test_approve_fails_if_not_awaiting(self):
        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.COMPLETED,
        )
        response = self.client.post(f"/api/skills/executions/{execution.id}/approve/", {})
        self.assertEqual(response.status_code, 400)

    @patch("apps.skill.dispatch.run_skill_task")
    def test_regenerate_step_returns_202(self, mock_task):
        mock_task.delay = mock_task
        execution = self._awaiting_execution()
        response = self.client.post(f"/api/skills/executions/{execution.id}/regenerate-step/")
        self.assertEqual(response.status_code, 202)
        execution.refresh_from_db()
        self.assertEqual(execution.status, ExecutionStatus.PENDING)
        self.assertEqual(execution.output_structured.get("steps", []), [])

    def test_regenerate_step_fails_if_not_awaiting(self):
        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            status=ExecutionStatus.RUNNING,
        )
        response = self.client.post(f"/api/skills/executions/{execution.id}/regenerate-step/")
        self.assertEqual(response.status_code, 400)

    def test_other_user_cannot_approve(self):
        other_user = User.objects.create_user(
            email="other@example.com", password="secret", username="other", role="admin")
        other_client = APIClient()
        other_client.force_authenticate(user=other_user)
        execution = self._awaiting_execution()
        response = other_client.post(f"/api/skills/executions/{execution.id}/approve/", {})
        self.assertEqual(response.status_code, 404)
