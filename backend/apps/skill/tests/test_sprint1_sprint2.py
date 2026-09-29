"""
Tests for Sprint 1 (tool use) and Sprint 2 (research phase + typed inputs).
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument
from apps.skill.models import (
    ExecutionOutputMode,
    Skill,
    SkillExecution,
    SkillParameter,
    SkillParameterType,
    SkillStep,
    SkillType,
)
from apps.skill.services import _render_prompt_variables, execute_skill
from apps.skill.tools import (
    SkillToolContext,
    _execute_calculate_ghg,
    _execute_get_document_list,
    _execute_search_more_context,
    execute_tool,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_chunk_id_counter = 0

def _make_chunk(doc, index=0):
    global _chunk_id_counter
    _chunk_id_counter += 1
    return SimpleNamespace(id=_chunk_id_counter, document=doc, chunk_index=index, content="chunk content")


# ---------------------------------------------------------------------------
# Sprint 2B — _render_prompt_variables
# ---------------------------------------------------------------------------

class RenderPromptVariablesTestCase(TestCase):
    def test_replaces_context_and_extra_instructions(self):
        tmpl = "Context: {{context}}\nInstructions: {{extra_instructions}}"
        result = _render_prompt_variables(
            tmpl,
            context_block="DOC CONTENT",
            extra_instructions="Focus on scope 2.",
            input_values={},
        )
        self.assertIn("DOC CONTENT", result)
        self.assertIn("Focus on scope 2.", result)

    def test_replaces_typed_parameters(self):
        tmpl = "Framework: {{framework}}\nYear: {{target_year}}\nCountry: {{country}}"
        result = _render_prompt_variables(
            tmpl,
            context_block="",
            extra_instructions="",
            input_values={"framework": "GRI", "target_year": "2024", "country": "Argentina"},
        )
        self.assertIn("GRI", result)
        self.assertIn("2024", result)
        self.assertIn("Argentina", result)

    def test_empty_context_uses_fallback(self):
        tmpl = "{{context}}"
        result = _render_prompt_variables(
            tmpl, context_block="", extra_instructions="", input_values={}
        )
        self.assertIn("No document content found", result)

    def test_none_input_value_renders_empty_string(self):
        tmpl = "Value: {{mykey}}"
        result = _render_prompt_variables(
            tmpl, context_block="", extra_instructions="", input_values={"mykey": None}
        )
        self.assertEqual(result, "Value:")

    def test_unknown_tokens_left_in_place(self):
        tmpl = "{{unknown_token}} and {{context}}"
        result = _render_prompt_variables(
            tmpl, context_block="CTX", extra_instructions="", input_values={}
        )
        self.assertIn("{{unknown_token}}", result)
        self.assertIn("CTX", result)


# ---------------------------------------------------------------------------
# Sprint 2B — SkillParameter model
# ---------------------------------------------------------------------------

class SkillParameterModelTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="params@example.com", password="secret123", username="params"
        )
        self.skill = Skill.objects.create(
            owner=self.user,
            name="Typed Skill",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            prompt_template="Framework: {{framework}}\n{{context}}",
        )

    def test_create_parameter(self):
        param = SkillParameter.objects.create(
            skill=self.skill,
            key="framework",
            label="Reporting Framework",
            param_type=SkillParameterType.ENUM,
            options=["GRI", "ISSB", "CDP"],
            required=True,
            position=1,
        )
        self.assertEqual(param.key, "framework")
        self.assertEqual(param.options, ["GRI", "ISSB", "CDP"])

    def test_unique_key_per_skill(self):
        SkillParameter.objects.create(skill=self.skill, key="framework", label="FW", position=1)
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            SkillParameter.objects.create(skill=self.skill, key="framework", label="FW2", position=2)


# ---------------------------------------------------------------------------
# Sprint 2B — input_values flow through Quick execution
# ---------------------------------------------------------------------------

class QuickSkillWithInputValuesTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="iv@example.com", password="secret123", username="iv"
        )
        self.project = Project.objects.create(owner=self.user, name="IVProject")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-iv")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="GHG Report",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            system_prompt="You are an ESG analyst.",
            prompt_template=(
                "Framework: {{framework}}\nTarget year: {{target_year}}\n"
                "Context:\n{{context}}\n{{extra_instructions}}"
            ),
        )

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_input_values_rendered_into_prompt(self, mock_fetch, mock_completion):
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Report text", {"total_tokens": 10})

        execution = SkillExecution.objects.create(
            skill=self.skill,
            owner=self.user,
            project=self.project,
            input_values={"framework": "GRI 305", "target_year": "2025"},
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        rendered_prompt = mock_completion.call_args.args[0][1]["content"]
        self.assertIn("GRI 305", rendered_prompt)
        self.assertIn("2025", rendered_prompt)


# ---------------------------------------------------------------------------
# Sprint 1 — Tool executors (unit tests, no DB)
# ---------------------------------------------------------------------------

class GhgCalculatorToolTestCase(TestCase):
    def _ctx(self):
        return SkillToolContext(user=None, allowed_documents=None)

    def test_basic_calculation(self):
        result = _execute_calculate_ghg(
            {"activity_data": 1000, "emission_factor": 0.000233, "unit": "kWh", "scope": "scope_2"},
            self._ctx(),
        )
        self.assertIn("0.233 tCO2e", result)
        self.assertIn("Scope 2", result)
        self.assertIn("kWh", result)

    def test_missing_required_field(self):
        result = _execute_calculate_ghg({"activity_data": 100, "unit": "km"}, self._ctx())
        self.assertIn("error", result.lower())

    def test_zero_activity(self):
        result = _execute_calculate_ghg(
            {"activity_data": 0, "emission_factor": 0.5, "unit": "kg"}, self._ctx()
        )
        self.assertIn("0", result)

    def test_with_description(self):
        result = _execute_calculate_ghg(
            {
                "activity_data": 500,
                "emission_factor": 0.002,
                "unit": "litres",
                "description": "Diesel fleet",
            },
            self._ctx(),
        )
        self.assertIn("Diesel fleet", result)


class SearchMoreContextToolTestCase(TestCase):
    def _ctx(self, chunks=None):
        ctx = SkillToolContext(user=MagicMock(), allowed_documents=MagicMock())
        return ctx

    def test_empty_query_returns_error(self):
        result = _execute_search_more_context({"query": ""}, self._ctx())
        self.assertIn("Error", result)

    @patch("apps.skill.tools.fetch_relevant_chunks")
    @patch("apps.skill.tools.build_context_block")
    def test_returns_context_block_on_success(self, mock_build, mock_fetch):
        chunk = MagicMock()
        chunk.id = 999
        mock_fetch.return_value = [chunk]
        mock_build.return_value = "FOUND CONTENT"
        ctx = self._ctx()
        result = _execute_search_more_context({"query": "scope 1 emissions"}, ctx)
        self.assertEqual(result, "FOUND CONTENT")
        self.assertEqual(len(ctx.additional_chunks), 1)

    @patch("apps.skill.tools.fetch_relevant_chunks")
    def test_no_results_returns_message(self, mock_fetch):
        mock_fetch.return_value = []
        result = _execute_search_more_context({"query": "obscure query"}, self._ctx())
        self.assertIn("No additional", result)

    @patch("apps.skill.tools.fetch_relevant_chunks")
    def test_top_n_capped_at_8(self, mock_fetch):
        mock_fetch.return_value = []
        self._execute_search_more_context_with_n(mock_fetch, top_n=99, expected_top_n=8)

    def _execute_search_more_context_with_n(self, mock_fetch, top_n, expected_top_n):
        ctx = self._ctx()
        _execute_search_more_context({"query": "test", "top_n": top_n}, ctx)
        call_kwargs = mock_fetch.call_args.kwargs
        self.assertEqual(call_kwargs["top_n"], expected_top_n)


class GetDocumentListToolTestCase(TestCase):
    def test_returns_document_names(self):
        doc1 = SimpleNamespace(id=1, slug="doc-a", name="Annual Report 2024")
        doc2 = SimpleNamespace(id=2, slug="doc-b", name="Sustainability Policy")
        mock_qs = MagicMock()
        mock_qs.only.return_value = [doc1, doc2]
        ctx = SkillToolContext(user=None, allowed_documents=mock_qs)
        result = _execute_get_document_list({}, ctx)
        self.assertIn("Annual Report 2024", result)
        self.assertIn("Sustainability Policy", result)
        self.assertIn("doc-a", result)

    def test_empty_corpus_message(self):
        mock_qs = MagicMock()
        mock_qs.only.return_value = []
        ctx = SkillToolContext(user=None, allowed_documents=mock_qs)
        result = _execute_get_document_list({}, ctx)
        self.assertIn("No documents", result)


class ExecuteToolDispatcherTestCase(TestCase):
    def test_unknown_tool_returns_error(self):
        ctx = SkillToolContext(user=None, allowed_documents=None)
        result = execute_tool("nonexistent_tool", "{}", ctx)
        self.assertIn("Unknown tool", result)

    def test_invalid_json_args(self):
        ctx = SkillToolContext(user=None, allowed_documents=None)
        result = execute_tool("calculate_ghg_emissions", "not-json", ctx)
        self.assertIn("Invalid JSON", result)

    def test_ghg_tool_dispatched(self):
        ctx = SkillToolContext(user=None, allowed_documents=None)
        args = json.dumps({"activity_data": 100, "emission_factor": 0.1, "unit": "km"})
        result = execute_tool("calculate_ghg_emissions", args, ctx)
        self.assertIn("tCO2e", result)


# ---------------------------------------------------------------------------
# Sprint 1 — generate_with_tools loop (unit test without DB)
# ---------------------------------------------------------------------------

class GenerateWithToolsTestCase(TestCase):
    def _make_response(self, finish_reason, content=None, tool_calls=None):
        choice = MagicMock()
        choice.finish_reason = finish_reason
        choice.message.content = content
        choice.message.tool_calls = tool_calls or []
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5
        resp.usage.total_tokens = 15
        return resp

    def _make_tool_call(self, name, args_dict, call_id="tc_1"):
        tc = MagicMock()
        tc.id = call_id
        tc.function.name = name
        tc.function.arguments = json.dumps(args_dict)
        tc.model_dump.return_value = {
            "id": call_id, "function": {"name": name, "arguments": tc.function.arguments}
        }
        return tc

    @patch("apps.document.utils.client_openia.get_openai_client")
    def test_no_tool_call_returns_directly(self, mock_get_client):
        stop_resp = self._make_response("stop", content="Final answer.")
        mock_get_client.return_value.chat.completions.create.return_value = stop_resp

        from apps.document.utils.client_openia import generate_with_tools
        text, usage = generate_with_tools(
            [{"role": "user", "content": "Hello"}],
            tools=[],
            tool_executor=lambda n, a: "result",
        )
        self.assertEqual(text, "Final answer.")
        self.assertEqual(usage["total_tokens"], 15)

    @patch("apps.document.utils.client_openia.get_openai_client")
    def test_single_tool_call_then_stop(self, mock_get_client):
        tc = self._make_tool_call(
            "calculate_ghg_emissions",
            {"activity_data": 100, "emission_factor": 0.5, "unit": "km"},
        )
        tool_resp = self._make_response("tool_calls", content=None, tool_calls=[tc])
        stop_resp = self._make_response("stop", content="Emissions are 50 tCO2e.")

        mock_client = mock_get_client.return_value
        mock_client.chat.completions.create.side_effect = [tool_resp, stop_resp]

        from apps.document.utils.client_openia import generate_with_tools

        calls_made = []

        def executor(name, args_json):
            calls_made.append(name)
            return "50 tCO2e result"

        text, usage = generate_with_tools(
            [{"role": "user", "content": "Calculate emissions."}],
            tools=[],
            tool_executor=executor,
        )

        self.assertEqual(text, "Emissions are 50 tCO2e.")
        self.assertEqual(calls_made, ["calculate_ghg_emissions"])
        # Two API calls: tool round + final
        self.assertEqual(mock_client.chat.completions.create.call_count, 2)
        # Accumulated usage
        self.assertEqual(usage["total_tokens"], 30)

    @patch("apps.document.utils.client_openia.get_openai_client")
    def test_max_iterations_forces_final_call(self, mock_get_client):
        tc = self._make_tool_call("search_more_context", {"query": "test"}, call_id="tc_x")
        tool_resp = self._make_response("tool_calls", tool_calls=[tc])
        stop_resp = self._make_response("stop", content="Done after max iters.")

        mock_client = mock_get_client.return_value
        # Always return tool_calls until the forced final call
        mock_client.chat.completions.create.side_effect = [
            tool_resp, tool_resp, stop_resp  # 2 iters + forced final
        ]

        from apps.document.utils.client_openia import generate_with_tools

        text, _ = generate_with_tools(
            [{"role": "user", "content": "Go."}],
            tools=[],
            tool_executor=lambda n, a: "result",
            max_iterations=2,
        )
        self.assertEqual(text, "Done after max iters.")


# ---------------------------------------------------------------------------
# Sprint 1 — tools_enabled wires through Quick runner
# ---------------------------------------------------------------------------

class QuickSkillToolsEnabledTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="tools@example.com", password="secret123", username="tools"
        )
        self.project = Project.objects.create(owner=self.user, name="ToolsProject")
        self.doc = Document.objects.create(owner=self.user, name="Doc", slug="doc-tools")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

    @patch("apps.skill.services.generate_with_tools")
    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_tools_enabled_uses_generate_with_tools(
        self, mock_fetch, mock_completion, mock_with_tools
    ):
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_with_tools.return_value = ("Agentic result", {"total_tokens": 20})
        # generate_chat_completion should NOT be called
        mock_completion.return_value = ("Should not appear", {"total_tokens": 0})

        skill = Skill.objects.create(
            owner=self.user,
            name="Agentic Quick",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            prompt_template="{{context}}",
            tools_enabled=True,
        )
        execution = SkillExecution.objects.create(
            skill=skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        self.assertEqual(execution.output, "Agentic result")
        mock_with_tools.assert_called_once()
        mock_completion.assert_not_called()

    @patch("apps.skill.services.generate_with_tools")
    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_tools_disabled_uses_generate_chat_completion(
        self, mock_fetch, mock_completion, mock_with_tools
    ):
        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Classic result", {"total_tokens": 5})

        skill = Skill.objects.create(
            owner=self.user,
            name="Classic Quick",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            prompt_template="{{context}}",
            tools_enabled=False,
        )
        execution = SkillExecution.objects.create(
            skill=skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        mock_completion.assert_called_once()
        mock_with_tools.assert_not_called()


# ---------------------------------------------------------------------------
# Sprint 2A — Research phase in Copilot runner
# ---------------------------------------------------------------------------

class CopilotResearchPhaseTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="research@example.com", password="secret123", username="research"
        )
        self.project = Project.objects.create(owner=self.user, name="ResearchProject")
        self.doc = Document.objects.create(owner=self.user, name="EIA Report", slug="doc-eia")
        ProjectDocument.objects.create(project=self.project, document=self.doc, added_by=self.user)

        self.skill = Skill.objects.create(
            owner=self.user,
            name="ESAP Workflow",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="ESG analyst.",
            research_phase_enabled=True,
        )
        SkillStep.objects.create(
            skill=self.skill, title="Scope", instructions="Define project scope.", position=1
        )
        SkillStep.objects.create(
            skill=self.skill, title="Risks", instructions="Identify environmental risks.", position=2
        )

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_research_phase_fetches_before_steps(self, mock_fetch, mock_completion):
        # Each call must return distinct chunk ids to avoid dedup dropping them
        call_counter = [0]
        def make_fresh_chunk(*args, **kwargs):
            call_counter[0] += 1
            return [SimpleNamespace(id=call_counter[0] * 100, document=self.doc, chunk_index=0, content="c")]
        mock_fetch.side_effect = make_fresh_chunk
        mock_completion.return_value = ("Step output", {"total_tokens": 5})

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, "completed")
        # Research phase (≥1 query) + 2 steps = at least 3 fetch calls
        self.assertGreaterEqual(mock_fetch.call_count, 3)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_research_phase_scratchpad_appears_in_step_prompt(self, mock_fetch, mock_completion):
        call_counter = [0]
        def make_fresh_chunk(*args, **kwargs):
            call_counter[0] += 1
            return [SimpleNamespace(id=call_counter[0] * 100, document=self.doc, chunk_index=0, content="c")]
        mock_fetch.side_effect = make_fresh_chunk
        mock_completion.return_value = ("Step output", {"total_tokens": 5})

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)

        # The first step's prompt should contain the research scratchpad section label
        first_step_prompt = mock_completion.call_args_list[0].args[0][1]["content"]
        self.assertIn("Relevamiento previo del expediente", first_step_prompt)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_research_phase_uses_explicit_queries_when_set(self, mock_fetch, mock_completion):
        self.skill.research_queries = ["What are the biodiversity risks?"]
        self.skill.save()

        call_counter = [0]
        def make_fresh_chunk(*args, **kwargs):
            call_counter[0] += 1
            return [SimpleNamespace(id=call_counter[0] * 100, document=self.doc, chunk_index=0, content="c")]
        mock_fetch.side_effect = make_fresh_chunk
        mock_completion.return_value = ("Output", {"total_tokens": 5})

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)

        # One of the fetch calls must use the explicit query
        all_queries = [c.kwargs["query_text"] for c in mock_fetch.call_args_list]
        self.assertIn("What are the biodiversity risks?", all_queries)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_research_phase_disabled_skips_pre_fetch(self, mock_fetch, mock_completion):
        self.skill.research_phase_enabled = False
        self.skill.save()

        mock_fetch.return_value = [_make_chunk(self.doc)]
        mock_completion.return_value = ("Step output", {"total_tokens": 5})

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)

        # Exactly 2 fetch calls (one per step), no research phase
        self.assertEqual(mock_fetch.call_count, 2)
        first_step_prompt = mock_completion.call_args_list[0].args[0][1]["content"]
        self.assertNotIn("Relevamiento previo del expediente", first_step_prompt)

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_research_metadata_recorded(self, mock_fetch, mock_completion):
        call_counter = [0]
        def make_fresh_chunk(*args, **kwargs):
            call_counter[0] += 1
            return [SimpleNamespace(id=call_counter[0] * 100, document=self.doc, chunk_index=0, content="c")]
        mock_fetch.side_effect = make_fresh_chunk
        mock_completion.return_value = ("Output", {"total_tokens": 5})

        execution = SkillExecution.objects.create(
            skill=self.skill, owner=self.user, project=self.project
        )
        execute_skill(execution)
        execution.refresh_from_db()

        self.assertTrue(execution.metadata.get("research_phase_enabled"))
