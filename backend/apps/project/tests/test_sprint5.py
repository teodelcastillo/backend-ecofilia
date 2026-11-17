"""
Tests for Sprint 5: Ecofilia Copilot + project structure.

Covers:
- ProjectStructureTemplate / ProjectSection models.
- initialize_project_structure service function.
- Copilot system prompt builder.
- Copilot tool executors (search_documents, calculate_ghg, get_document_list,
  run_skill, get_execution_history, get_project_structure, update_section_status).
- Copilot message processing (generate_with_tools mock).
- API endpoints: structure, copilot sessions, copilot messages.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from apps.chat.models import ChatSession, ChatSessionType
from apps.document.models import Document
from apps.project.models import (
    Project,
    ProjectDeliverable,
    ProjectDocument,
    ProjectSection,
    ProjectSectionStatus,
    ProjectStructureSection,
    ProjectStructureTemplate,
)

User = get_user_model()

_chunk_id_counter = 0


def _section(project, **fields):
    """Una sección del entregable principal del proyecto.

    Desde que un proyecto tiene varios entregables (mayo de 2026) una sección
    cuelga de un entregable: la posición es única por entregable y la API de
    estructura resuelve las secciones del principal. Una sección suelta, sin
    entregable, no la encuentra nadie.
    """
    deliverable = ProjectDeliverable.objects.filter(project=project, is_primary=True).first()
    if deliverable is None:
        deliverable = ProjectDeliverable.objects.create(
            project=project, name="Entregable principal", is_primary=True, position=1,
        )
    return ProjectSection.objects.create(project=project, deliverable=deliverable, **fields)


def _isolate_copilot_retrieval(testcase):
    """El copilot arma un resumen del proyecto y recomienda documentos de la
    biblioteca con recuperación real (embeddings). Un test de mensajes no tiene
    por qué salir a la red: se fijan los dos y se limpia el caché del resumen."""
    from django.core.cache import cache

    cache.clear()
    brief = patch(
        "apps.chat.services.copilot.build_project_brief",
        return_value="Resumen del proyecto.",
    )
    recommendations = patch(
        "apps.chat.services.copilot.suggest_related_library_documents",
        return_value=[],
    )
    testcase.mock_brief = brief.start()
    recommendations.start()
    testcase.addCleanup(brief.stop)
    testcase.addCleanup(recommendations.stop)


def _make_chunk(doc, index=0):
    global _chunk_id_counter
    _chunk_id_counter += 1
    return SimpleNamespace(
        id=_chunk_id_counter, document=doc, chunk_index=index, content="chunk"
    )


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class StructureTemplateModelTestCase(TestCase):
    def test_create_template_with_sections(self):
        template = ProjectStructureTemplate.objects.create(
            name="GEI Test", slug="gei-test", description="Test template."
        )
        ProjectStructureSection.objects.create(
            template=template, title="Scope", position=1
        )
        ProjectStructureSection.objects.create(
            template=template, title="Data", position=2
        )
        self.assertEqual(template.sections.count(), 2)
        self.assertEqual(str(template), "GEI Test")


class ProjectSectionModelTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="sec@example.com", password="secret123", username="sec"
        )
        self.project = Project.objects.create(owner=self.user, name="SecProject")

    def test_create_project_section(self):
        section = _section(self.project, title="Intro", position=1)
        self.assertEqual(section.status, ProjectSectionStatus.NOT_STARTED)

    def test_status_transitions(self):
        section = _section(self.project, title="Intro", position=1)
        section.status = ProjectSectionStatus.IN_PROGRESS
        section.save(update_fields=["status"])
        section.refresh_from_db()
        self.assertEqual(section.status, ProjectSectionStatus.IN_PROGRESS)

    def test_unique_position_per_deliverable(self):
        _section(self.project, title="A", position=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            _section(self.project, title="B", position=1)


# ---------------------------------------------------------------------------
# Service: initialize_project_structure
# ---------------------------------------------------------------------------


class InitializeProjectStructureTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="init@example.com", password="secret123", username="init"
        )
        self.project = Project.objects.create(owner=self.user, name="InitProject")
        self.template = ProjectStructureTemplate.objects.create(
            name="Template", slug="init-template"
        )
        ProjectStructureSection.objects.create(
            template=self.template, title="Step A", position=1, description="Do A."
        )
        ProjectStructureSection.objects.create(
            template=self.template, title="Step B", position=2, description="Do B."
        )

    def test_initializes_sections_from_template(self):
        from apps.chat.services.copilot import initialize_project_structure

        sections = initialize_project_structure(self.project, "init-template")
        self.assertEqual(len(sections), 2)
        self.assertEqual(sections[0].title, "Step A")
        self.assertEqual(sections[1].title, "Step B")
        self.project.refresh_from_db()
        self.assertEqual(self.project.structure_template, self.template)

    def test_reinitialize_replaces_existing(self):
        from apps.chat.services.copilot import initialize_project_structure

        _section(self.project, title="Old", position=1)
        sections = initialize_project_structure(self.project, "init-template")
        self.assertEqual(len(sections), 2)
        self.assertEqual(ProjectSection.objects.filter(project=self.project).count(), 2)


# ---------------------------------------------------------------------------
# Copilot system prompt
# ---------------------------------------------------------------------------


class CopilotSystemPromptTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="prompt@example.com", password="secret123", username="prompt"
        )
        self.project = Project.objects.create(
            owner=self.user,
            name="PromptProject",
            context_notes={"company": "Acme Corp", "sector": "Manufacturing"},
        )
        self.doc = Document.objects.create(
            owner=self.user, name="Report", slug="report-prompt"
        )
        ProjectDocument.objects.create(
            project=self.project, document=self.doc, added_by=self.user
        )
        _section(self.project, title="Scope", position=1,
            status=ProjectSectionStatus.COMPLETED,
        )
        _section(self.project, title="Data", position=2,
            status=ProjectSectionStatus.IN_PROGRESS, notes="Missing Q4",
        )

    def test_includes_project_name(self):
        from apps.chat.services.copilot import build_copilot_system_prompt

        documents = Document.objects.filter(id=self.doc.id)
        prompt = build_copilot_system_prompt(self.project, documents)
        self.assertIn("PromptProject", prompt)

    def test_includes_context_notes(self):
        from apps.chat.services.copilot import build_copilot_system_prompt

        documents = Document.objects.filter(id=self.doc.id)
        prompt = build_copilot_system_prompt(self.project, documents)
        self.assertIn("Acme Corp", prompt)
        self.assertIn("Manufacturing", prompt)

    def test_includes_structure_with_statuses(self):
        from apps.chat.services.copilot import build_copilot_system_prompt

        documents = Document.objects.filter(id=self.doc.id)
        prompt = build_copilot_system_prompt(self.project, documents)
        self.assertIn("[COMPLETADO] Scope", prompt)
        self.assertIn("[EN PROGRESO] Data", prompt)
        self.assertIn("Missing Q4", prompt)

    def test_includes_document_list(self):
        from apps.chat.services.copilot import build_copilot_system_prompt

        documents = Document.objects.filter(id=self.doc.id)
        prompt = build_copilot_system_prompt(self.project, documents)
        self.assertIn("report-prompt", prompt)

    def test_marks_blueprint_when_set(self):
        from apps.chat.services.copilot import build_copilot_system_prompt

        self.project.blueprint_document = self.doc
        self.project.save(update_fields=["blueprint_document"])
        documents = Document.objects.filter(id=self.doc.id)
        prompt = build_copilot_system_prompt(self.project, documents)
        self.assertIn("[BLUEPRINT]", prompt)
        self.assertIn("Documento principal (blueprint)", prompt)

    def test_includes_draft_delimiters_instruction(self):
        from apps.chat.services.copilot import build_copilot_system_prompt

        documents = Document.objects.filter(id=self.doc.id)
        prompt = build_copilot_system_prompt(self.project, documents)
        self.assertIn("<<<DRAFT", prompt)
        self.assertIn("<<<END>>>", prompt)


# ---------------------------------------------------------------------------
# Copilot tools
# ---------------------------------------------------------------------------


class CopilotSearchDocumentsToolTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="search@example.com", password="secret123", username="search"
        )
        self.project = Project.objects.create(owner=self.user, name="SearchProject")
        self.doc = Document.objects.create(
            owner=self.user, name="Doc", slug="doc-search"
        )
        ProjectDocument.objects.create(
            project=self.project, document=self.doc, added_by=self.user
        )

    @patch("apps.chat.services.copilot_tools.build_context_block")
    @patch("apps.chat.services.copilot_tools.fetch_relevant_chunks")
    def test_returns_context_block(self, mock_fetch, mock_build):
        from apps.chat.services.copilot_tools import CopilotToolContext, _execute_search_documents

        chunk = _make_chunk(self.doc)
        mock_fetch.return_value = [chunk]
        mock_build.return_value = "Context block content."

        ctx = CopilotToolContext(
            user=self.user,
            project=self.project,
            allowed_documents=Document.objects.filter(id=self.doc.id),
        )
        result = _execute_search_documents({"query": "emissions"}, ctx)
        self.assertEqual(result, "Context block content.")
        self.assertEqual(len(ctx.additional_chunks), 1)

    def test_empty_query_returns_error(self):
        from apps.chat.services.copilot_tools import CopilotToolContext, _execute_search_documents

        ctx = CopilotToolContext(
            user=self.user,
            project=self.project,
            allowed_documents=Document.objects.filter(id=self.doc.id),
        )
        result = _execute_search_documents({"query": ""}, ctx)
        self.assertIn("Error", result)


class CopilotRunSkillToolTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="rskill@example.com", password="secret123", username="rskill"
        )
        self.project = Project.objects.create(owner=self.user, name="SkillProject")
        self.doc = Document.objects.create(
            owner=self.user, name="Doc", slug="doc-rskill"
        )
        ProjectDocument.objects.create(
            project=self.project, document=self.doc, added_by=self.user
        )

    @patch("apps.skill.services.execute_skill")
    def test_runs_quick_skill(self, mock_execute):
        from apps.skill.models import Skill, SkillExecution, SkillType, ExecutionStatus
        from apps.chat.services.copilot_tools import CopilotToolContext, _execute_run_skill

        skill = Skill.objects.create(
            owner=self.user,
            name="Quick Analysis",
            # Abierta a no superadmins: desde "Sólo superadmins ejecutan
            # asistentes" un usuario común no corre nada que no lo permita.
            members_can_run=True,
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            prompt_template="Analyze {{context}}",
        )

        mock_execute.return_value = SimpleNamespace(
            status=ExecutionStatus.COMPLETED,
            output="Analysis result.",
            error_message="",
        )

        ctx = CopilotToolContext(
            user=self.user,
            project=self.project,
            allowed_documents=Document.objects.filter(id=self.doc.id),
        )
        result = _execute_run_skill({"skill_slug": skill.slug}, ctx)
        self.assertIn("Analysis result.", result)
        mock_execute.assert_called_once()

    def test_rejects_copilot_skill(self):
        from apps.skill.models import Skill, SkillType
        from apps.chat.services.copilot_tools import CopilotToolContext, _execute_run_skill

        skill = Skill.objects.create(
            owner=self.user,
            name="Copilot Skill",
            # Abierta a no superadmins: desde "Sólo superadmins ejecutan
            # asistentes" un usuario común no corre nada que no lo permita.
            members_can_run=True,
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="ESG.",
        )

        ctx = CopilotToolContext(
            user=self.user,
            project=self.project,
            allowed_documents=Document.objects.filter(id=self.doc.id),
        )
        result = _execute_run_skill({"skill_slug": skill.slug}, ctx)
        self.assertIn("cannot be run inline", result)

    def test_unknown_skill_returns_error(self):
        from apps.chat.services.copilot_tools import CopilotToolContext, _execute_run_skill

        ctx = CopilotToolContext(
            user=self.user,
            project=self.project,
            allowed_documents=Document.objects.filter(id=self.doc.id),
        )
        result = _execute_run_skill({"skill_slug": "nonexistent"}, ctx)
        self.assertIn("not found", result)


class CopilotProjectStructureToolTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="struct@example.com", password="secret123", username="struct"
        )
        self.project = Project.objects.create(owner=self.user, name="StructProject")
        _section(self.project, title="Scope", position=1,
            status=ProjectSectionStatus.COMPLETED,
        )
        _section(self.project, title="Data", position=2,
            status=ProjectSectionStatus.NOT_STARTED,
        )

    def test_get_project_structure(self):
        from apps.chat.services.copilot_tools import CopilotToolContext, _execute_get_project_structure

        ctx = CopilotToolContext(
            user=self.user,
            project=self.project,
            allowed_documents=Document.objects.none(),
        )
        result = _execute_get_project_structure({}, ctx)
        self.assertIn("COMPLETADO", result)
        self.assertIn("PENDIENTE", result)
        self.assertIn("Scope", result)

    def test_update_section_status(self):
        from apps.chat.services.copilot_tools import CopilotToolContext, _execute_update_section_status

        ctx = CopilotToolContext(
            user=self.user,
            project=self.project,
            allowed_documents=Document.objects.none(),
        )
        result = _execute_update_section_status(
            {"section_position": 2, "status": "in_progress", "notes": "Started."}, ctx
        )
        self.assertIn("updated", result)
        section = ProjectSection.objects.get(project=self.project, position=2)
        self.assertEqual(section.status, ProjectSectionStatus.IN_PROGRESS)
        self.assertEqual(section.notes, "Started.")

    def test_update_invalid_position(self):
        from apps.chat.services.copilot_tools import CopilotToolContext, _execute_update_section_status

        ctx = CopilotToolContext(
            user=self.user,
            project=self.project,
            allowed_documents=Document.objects.none(),
        )
        result = _execute_update_section_status(
            {"section_position": 99, "status": "completed"}, ctx
        )
        self.assertIn("No section", result)


class CopilotToolDispatcherTestCase(TestCase):
    def test_unknown_tool(self):
        from apps.chat.services.copilot_tools import CopilotToolContext, execute_copilot_tool

        user = User.objects.create_user(
            email="disp@example.com", password="secret123", username="disp"
        )
        project = Project.objects.create(owner=user, name="DispProject")
        ctx = CopilotToolContext(
            user=user, project=project, allowed_documents=Document.objects.none(),
        )
        result = execute_copilot_tool("bogus_tool", "{}", ctx)
        self.assertIn("Unknown tool", result)

    def test_invalid_json_args(self):
        from apps.chat.services.copilot_tools import CopilotToolContext, execute_copilot_tool

        user = User.objects.create_user(
            email="json@example.com", password="secret123", username="jsonuser"
        )
        project = Project.objects.create(owner=user, name="JsonProject")
        ctx = CopilotToolContext(
            user=user, project=project, allowed_documents=Document.objects.none(),
        )
        result = execute_copilot_tool("search_documents", "not json!", ctx)
        self.assertIn("Invalid JSON", result)


# ---------------------------------------------------------------------------
# Copilot message processing (end-to-end with mocks)
# ---------------------------------------------------------------------------


class CopilotMessageProcessingTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="msg@example.com", password="secret123", username="msg"
        )
        self.project = Project.objects.create(
            owner=self.user, name="MsgProject",
            context_notes={"company": "TestCo"},
            copilot_enabled=True,
        )
        self.doc = Document.objects.create(
            owner=self.user, name="Doc", slug="doc-msg"
        )
        ProjectDocument.objects.create(
            project=self.project, document=self.doc, added_by=self.user
        )
        self.session = ChatSession.objects.create(
            owner=self.user,
            project=self.project,
            session_type=ChatSessionType.COPILOT,
            title="Test Copilot",
        )
        self.session.allowed_documents.set([self.doc])
        _isolate_copilot_retrieval(self)

    @patch("apps.chat.services.copilot.generate_with_tools")
    def test_process_copilot_message(self, mock_gen):
        from apps.chat.services.copilot import process_copilot_message

        mock_gen.return_value = ("Here is my analysis.", {"total_tokens": 50})

        text, metadata, chunk_ids = process_copilot_message(
            self.session, "What emissions data do we have?", self.user
        )

        self.assertEqual(text, "Here is my analysis.")
        self.assertTrue(metadata["copilot"])
        self.assertNotIn("draft_markdown", metadata)
        mock_gen.assert_called_once()
        call_args = mock_gen.call_args
        messages = call_args.args[0]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("MsgProject", messages[0]["content"])
        self.assertIn("TestCo", messages[0]["content"])

    @patch("apps.chat.services.copilot.generate_with_tools")
    def test_extracts_draft_block_into_metadata(self, mock_gen):
        from apps.chat.services.copilot import process_copilot_message

        _section(self.project, title="Resumen", position=1)
        reply = (
            "Listo, te dejo un primer borrador.\n\n"
            "<<<DRAFT section=1>>>\n"
            "## Resumen ejecutivo\n\n"
            "Este informe sintetiza los hallazgos clave.\n"
            "<<<END>>>\n\n"
            "¿Querés que lo expanda?"
        )
        mock_gen.return_value = (reply, {"total_tokens": 70})

        _, metadata, _ = process_copilot_message(
            self.session, "Redactá el resumen.", self.user,
        )

        self.assertEqual(metadata["draft_section_position"], 1)
        self.assertIn("Resumen ejecutivo", metadata["draft_markdown"])
        section = ProjectSection.objects.get(project=self.project, position=1)
        self.assertIn("Resumen ejecutivo", section.output_snapshot)

    @patch("apps.chat.services.copilot.generate_with_tools")
    def test_extracts_draft_without_section_target(self, mock_gen):
        from apps.chat.services.copilot import process_copilot_message

        reply = "Aquí va.\n<<<DRAFT>>>\n# Título\n\nCuerpo.\n<<<END>>>"
        mock_gen.return_value = (reply, {"total_tokens": 40})

        _, metadata, _ = process_copilot_message(
            self.session, "Necesito un encabezado.", self.user,
        )
        self.assertIsNone(metadata["draft_section_position"])
        self.assertIn("Cuerpo", metadata["draft_markdown"])

    @patch("apps.chat.services.copilot.generate_with_tools")
    def test_project_brief_is_built_once_per_session(self, mock_gen):
        """El resumen se arma en el primer mensaje y se reusa en los siguientes.

        Antes se guardaba en ``session.metadata``, un campo que ``ChatSession``
        no tiene: cada mensaje caía con AttributeError y la API daba 503."""
        from apps.chat.services.copilot import process_copilot_message

        mock_gen.return_value = ("Ok.", {"total_tokens": 5})
        process_copilot_message(self.session, "Primera pregunta.", self.user)
        process_copilot_message(self.session, "Segunda pregunta.", self.user)

        self.assertEqual(self.mock_brief.call_count, 1)
        # Viaja como segundo mensaje de sistema, también en el segundo pedido.
        brief_message = mock_gen.call_args.args[0][1]
        self.assertEqual(brief_message["role"], "system")
        self.assertIn("Resumen del proyecto.", brief_message["content"])

    @patch("apps.chat.services.copilot.generate_with_tools")
    def test_no_project_raises_error(self, mock_gen):
        from apps.chat.services.copilot import process_copilot_message

        session = ChatSession.objects.create(
            owner=self.user,
            session_type=ChatSessionType.COPILOT,
            title="No Project",
        )
        with self.assertRaises(ValueError):
            process_copilot_message(session, "hello", self.user)


class ExtractDraftBlockTestCase(TestCase):
    def test_extracts_with_section_attribute(self):
        from apps.chat.services.copilot import extract_draft_block

        body, position = extract_draft_block(
            "lead\n<<<DRAFT section=3>>>\nMD body\n<<<END>>>"
        )
        self.assertEqual(body, "MD body")
        self.assertEqual(position, 3)

    def test_returns_none_when_no_block(self):
        from apps.chat.services.copilot import extract_draft_block

        body, position = extract_draft_block("plain text only")
        self.assertIsNone(body)
        self.assertIsNone(position)

    def test_returns_none_when_unclosed_block(self):
        from apps.chat.services.copilot import extract_draft_block

        body, position = extract_draft_block("<<<DRAFT>>> incomplete")
        self.assertIsNone(body)
        self.assertIsNone(position)

    def test_strips_padding_whitespace(self):
        from apps.chat.services.copilot import extract_draft_block

        body, _ = extract_draft_block("<<<DRAFT>>>\n\n  body  \n\n<<<END>>>")
        self.assertEqual(body, "body")


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


class StructureTemplateAPITestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="tmpl@example.com", password="secret123", username="tmpl"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.template = ProjectStructureTemplate.objects.create(
            name="API Template", slug="api-template", description="Desc."
        )
        ProjectStructureSection.objects.create(
            template=self.template, title="S1", position=1
        )
        self.user_template = ProjectStructureTemplate.objects.create(
            owner=self.user, name="My Template", slug="my-template", description="Mine."
        )
        ProjectStructureSection.objects.create(
            template=self.user_template, title="Mine S1", position=1
        )
        self.other = User.objects.create_user(
            email="tmpl-other@example.com", password="secret123", username="tmpl-other"
        )
        self.other_template = ProjectStructureTemplate.objects.create(
            owner=self.other, name="Other Template", slug="other-template", description="Other."
        )

    def test_list_templates(self):
        response = self.client.get("/api/projects/structure-templates/")
        self.assertEqual(response.status_code, 200)
        slugs = {row["slug"] for row in response.data}
        # Non-staff user sees global + own templates, not others' private templates.
        self.assertIn("api-template", slugs)
        self.assertIn("my-template", slugs)
        self.assertNotIn("other-template", slugs)

    def test_retrieve_template(self):
        response = self.client.get("/api/projects/structure-templates/api-template/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["slug"], "api-template")
        self.assertEqual(len(response.data["sections"]), 1)

    def test_create_template_with_sections(self):
        response = self.client.post(
            "/api/projects/structure-templates/",
            {
                "name": "Custom Template",
                "slug": "custom-template",
                "description": "Custom desc",
                "sections": [
                    {"title": "Intro", "description": "A", "position": 1},
                    {"title": "Resultados", "description": "B", "position": 2},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        created = ProjectStructureTemplate.objects.get(slug="custom-template")
        self.assertEqual(created.sections.count(), 2)

    def test_partial_update_template_replaces_sections(self):
        # Sobre la plantilla propia: las globales sólo las edita staff (ver
        # `test_non_staff_cannot_edit_global_template`).
        response = self.client.patch(
            "/api/projects/structure-templates/my-template/",
            {
                "description": "Updated desc",
                "sections": [
                    {"title": "Nuevo alcance", "description": "N", "position": 1},
                    {"title": "Nuevo cierre", "description": "C", "position": 2},
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user_template.refresh_from_db()
        self.assertEqual(self.user_template.description, "Updated desc")
        self.assertEqual(self.user_template.sections.count(), 2)
        self.assertTrue(
            self.user_template.sections.filter(title="Nuevo alcance", position=1).exists()
        )
        self.assertFalse(self.user_template.sections.filter(title="Mine S1").exists())

    def test_delete_template(self):
        response = self.client.delete("/api/projects/structure-templates/api-template/")
        self.assertEqual(response.status_code, 403)

    def test_non_staff_cannot_create_global_template(self):
        response = self.client.post(
            "/api/projects/structure-templates/",
            {
                "name": "Illegal Global",
                "slug": "illegal-global",
                "description": "Nope",
                "is_global": True,
                "sections": [{"title": "S1", "position": 1}],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_non_staff_cannot_edit_global_template(self):
        response = self.client.patch(
            "/api/projects/structure-templates/api-template/",
            {"description": "hack"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    def test_non_staff_cannot_delete_other_private_template(self):
        response = self.client.delete("/api/projects/structure-templates/other-template/")
        self.assertEqual(response.status_code, 404)

    def test_non_staff_can_edit_own_template(self):
        response = self.client.patch(
            "/api/projects/structure-templates/my-template/",
            {"description": "updated mine"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.user_template.refresh_from_db()
        self.assertEqual(self.user_template.description, "updated mine")

    def test_staff_can_manage_global_templates(self):
        admin = User.objects.create_user(
            email="tmpl-admin@example.com",
            password="secret123",
            username="tmpl-admin",
            role="admin",
            is_staff=True,
        )
        staff_client = APIClient()
        staff_client.force_authenticate(user=admin)

        create = staff_client.post(
            "/api/projects/structure-templates/",
            {
                "name": "Global By Staff",
                "slug": "global-by-staff",
                "is_global": True,
                "sections": [{"title": "S1", "position": 1}],
            },
            format="json",
        )
        self.assertEqual(create.status_code, 201)
        created = ProjectStructureTemplate.objects.get(slug="global-by-staff")
        self.assertIsNone(created.owner_id)

        patch = staff_client.patch(
            "/api/projects/structure-templates/api-template/",
            {"description": "staff updated"},
            format="json",
        )
        self.assertEqual(patch.status_code, 200)

        delete = staff_client.delete("/api/projects/structure-templates/api-template/")
        self.assertEqual(delete.status_code, 204)


class ProjectStructureAPITestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="pstr@example.com", password="secret123", username="pstr"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.project = Project.objects.create(owner=self.user, name="StructAPIProject")
        self.template = ProjectStructureTemplate.objects.create(
            name="Struct Template", slug="struct-template"
        )
        ProjectStructureSection.objects.create(
            template=self.template, title="Phase 1", position=1
        )
        ProjectStructureSection.objects.create(
            template=self.template, title="Phase 2", position=2
        )

    def test_get_empty_structure(self):
        response = self.client.get(
            f"/api/projects/{self.project.slug}/structure/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["sections"], [])

    def test_initialize_structure(self):
        response = self.client.put(
            f"/api/projects/{self.project.slug}/structure/initialize/",
            {"template_slug": "struct-template"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(response.data[0]["title"], "Phase 1")

    def test_update_section_status(self):
        _section(self.project, title="Phase 1", position=1)
        response = self.client.patch(
            f"/api/projects/{self.project.slug}/structure/sections/1/",
            {"status": "in_progress", "notes": "Working on it."},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "in_progress")
        self.assertEqual(response.data["notes"], "Working on it.")

    def test_update_section_title_and_output_snapshot(self):
        _section(self.project, title="Phase 1", position=1)
        response = self.client.patch(
            f"/api/projects/{self.project.slug}/structure/sections/1/",
            {
                "title": "Resumen ejecutivo",
                "description": "Síntesis del informe.",
                "output_snapshot": "## Resumen\n\nContenido inicial.",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["title"], "Resumen ejecutivo")
        self.assertEqual(response.data["description"], "Síntesis del informe.")
        self.assertIn("Contenido inicial", response.data["output_snapshot"])

    def test_create_section_at_end(self):
        response = self.client.post(
            f"/api/projects/{self.project.slug}/structure/sections/",
            {"title": "Anexos", "description": "Tablas y figuras."},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["position"], 1)
        self.assertEqual(response.data["title"], "Anexos")

    def test_create_section_inserted_in_middle_shifts_positions(self):
        _section(self.project, title="A", position=1)
        _section(self.project, title="B", position=2)
        response = self.client.post(
            f"/api/projects/{self.project.slug}/structure/sections/",
            {"title": "Inserted", "position": 2},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["position"], 2)
        positions = list(
            ProjectSection.objects.filter(project=self.project)
            .order_by("position").values_list("title", flat=True)
        )
        self.assertEqual(positions, ["A", "Inserted", "B"])

    def test_delete_section_compacts_positions(self):
        _section(self.project, title="A", position=1)
        _section(self.project, title="B", position=2)
        _section(self.project, title="C", position=3)
        response = self.client.delete(
            f"/api/projects/{self.project.slug}/structure/sections/2/"
        )
        self.assertEqual(response.status_code, 204)
        remaining = list(
            ProjectSection.objects.filter(project=self.project)
            .order_by("position").values_list("title", "position")
        )
        self.assertEqual(remaining, [("A", 1), ("C", 2)])


class CopilotSessionAPITestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="csess@example.com", password="secret123", username="csess"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.project = Project.objects.create(
            owner=self.user, name="CopilotSessionProject", copilot_enabled=True,
        )
        self.doc = Document.objects.create(
            owner=self.user, name="Doc", slug="doc-csess"
        )
        ProjectDocument.objects.create(
            project=self.project, document=self.doc, added_by=self.user
        )

    def test_create_copilot_session(self):
        response = self.client.post(
            f"/api/projects/{self.project.slug}/copilot/sessions/"
        )
        self.assertEqual(response.status_code, 201)
        session = ChatSession.objects.get(pk=response.data["id"])
        self.assertEqual(session.session_type, ChatSessionType.COPILOT)
        self.assertEqual(session.project, self.project)

    def test_list_copilot_sessions(self):
        ChatSession.objects.create(
            owner=self.user, project=self.project,
            session_type=ChatSessionType.COPILOT, title="S1",
        )
        ChatSession.objects.create(
            owner=self.user, project=self.project,
            session_type=ChatSessionType.STANDARD, title="Not copilot",
        )
        response = self.client.get(
            f"/api/projects/{self.project.slug}/copilot/sessions/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_other_user_cannot_create_session(self):
        other = User.objects.create_user(
            email="other@example.com", password="secret123", username="other"
        )
        other_client = APIClient()
        other_client.force_authenticate(user=other)
        response = other_client.post(
            f"/api/projects/{self.project.slug}/copilot/sessions/"
        )
        self.assertEqual(response.status_code, 404)


class CopilotMessageAPITestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="cmsg@example.com", password="secret123", username="cmsg"
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.project = Project.objects.create(
            owner=self.user, name="CopilotMsgProject",
            context_notes={"company": "TestCo"},
        )
        self.doc = Document.objects.create(
            owner=self.user, name="Doc", slug="doc-cmsg"
        )
        ProjectDocument.objects.create(
            project=self.project, document=self.doc, added_by=self.user
        )
        self.session = ChatSession.objects.create(
            owner=self.user, project=self.project,
            session_type=ChatSessionType.COPILOT, title="Test",
        )
        self.session.allowed_documents.set([self.doc])
        _isolate_copilot_retrieval(self)

    @patch("apps.chat.services.copilot.generate_with_tools")
    def test_send_copilot_message(self, mock_gen):
        mock_gen.return_value = ("Copilot response.", {"total_tokens": 10})
        response = self.client.post(
            f"/api/projects/{self.project.slug}/copilot/messages/",
            {"session": self.session.id, "content": "Help me with scope 1."},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.data["assistant_message"]["content"], "Copilot response."
        )
        self.assertEqual(response.data["user_message"]["content"], "Help me with scope 1.")

    @patch("apps.chat.services.copilot.generate_with_tools")
    def test_send_message_without_session_uses_latest(self, mock_gen):
        mock_gen.return_value = ("Response.", {"total_tokens": 5})
        response = self.client.post(
            f"/api/projects/{self.project.slug}/copilot/messages/",
            {"content": "What's next?"},
        )
        self.assertEqual(response.status_code, 201)

    def test_send_message_no_session_returns_400(self):
        self.session.delete()
        response = self.client.post(
            f"/api/projects/{self.project.slug}/copilot/messages/",
            {"content": "Hello"},
        )
        self.assertEqual(response.status_code, 400)
