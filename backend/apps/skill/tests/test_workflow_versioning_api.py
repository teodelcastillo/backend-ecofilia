"""
Fase 7 de punta a punta: versión de definición, repetir y comparar corridas.

Usa skills QUICK porque el runner asigna la versión de definición antes de
bifurcar a QUICK o COPILOT — no hace falta el motor de pasos para probar esto,
y mockear ``generate_chat_completion``/``fetch_relevant_chunks`` alcanza para
correr una ejecución completa de punta a punta, igual que en
``test_services.py``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument, ProjectShare, ProjectShareRole
from apps.skill.models import (
    Skill,
    SkillDefinitionVersion,
    SkillExecution,
    SkillParameter,
    SkillParameterType,
    SkillType,
)
from apps.skill.services import execute_skill

User = get_user_model()


def _run_synchronously(execution):
    """Evita depender de Celery en el test: corre la tarea inline."""
    execute_skill(execution)
    execution.refresh_from_db()
    return execution


class VersioningAPITestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="autor@example.com", password="secret123", username="autor",
        )
        self.client.force_authenticate(self.user)

        self.project = Project.objects.create(owner=self.user, name="Operación 34")
        self.doc = Document.objects.create(owner=self.user, name="IDO", slug="ido-34")
        ProjectDocument.objects.create(
            project=self.project, document=self.doc, added_by=self.user
        )

        self.skill = Skill.objects.create(
            owner=self.user,
            name="Resumen ejecutivo",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            system_prompt="Sé preciso.",
            prompt_template="Marco: {{marco}}\n\n{{context}}\n\n{{extra_instructions}}",
        )
        SkillParameter.objects.create(
            skill=self.skill,
            key="marco",
            label="Marco de referencia",
            param_type=SkillParameterType.ENUM,
            options=["GRI", "ISSB"],
            default_value="GRI",
            required=True,
        )
        self.project.enabled_skills.add(self.skill)

    def _run(self, **payload):
        body = {
            "context_type": "project",
            "context_slug": self.project.slug,
            **payload,
        }
        return self.client.post(
            reverse("skill-run", kwargs={"slug": self.skill.slug}), body, format="json"
        )

    # -- validación de parámetros al lanzar ------------------------------

    def test_run_is_rejected_when_enum_value_is_outside_the_vocabulary(self):
        response = self._run(input_values={"marco": "TCFD"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["parameter_issues"][0]["problem"], "invalid_enum")

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_run_applies_the_declared_default_when_omitted(
        self, mock_fetch_chunks, mock_completion
    ):
        mock_fetch_chunks.return_value = [
            SimpleNamespace(document=self.doc, chunk_index=0, content="x")
        ]
        mock_completion.return_value = ("Resultado", {"total_tokens": 5})

        response = self._run()

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["input_values"]["marco"], "GRI")
        rendered_prompt = mock_completion.call_args.args[0][1]["content"]
        self.assertIn("Marco: GRI", rendered_prompt)

    # -- versión de definición --------------------------------------------

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_completed_execution_is_pinned_to_a_definition_version(
        self, mock_fetch_chunks, mock_completion
    ):
        mock_fetch_chunks.return_value = [
            SimpleNamespace(document=self.doc, chunk_index=0, content="x")
        ]
        mock_completion.return_value = ("Resultado", {"total_tokens": 5})

        response = self._run()
        execution = SkillExecution.objects.get(pk=response.data["id"])

        self.assertIsNotNone(execution.definition_version_id)
        self.assertEqual(execution.definition_version.version_number, 1)
        self.assertEqual(
            execution.metadata["run_manifest"]["definition_version"], 1
        )

    @patch("apps.skill.services.generate_chat_completion")
    @patch("apps.skill.services.fetch_relevant_chunks")
    def test_editing_the_skill_between_runs_produces_a_new_version(
        self, mock_fetch_chunks, mock_completion
    ):
        mock_fetch_chunks.return_value = [
            SimpleNamespace(document=self.doc, chunk_index=0, content="x")
        ]
        mock_completion.return_value = ("Resultado", {"total_tokens": 5})

        first = SkillExecution.objects.get(pk=self._run().data["id"])

        self.skill.system_prompt = "Sé exhaustivo."
        self.skill.save(update_fields=["system_prompt"])

        second = SkillExecution.objects.get(pk=self._run().data["id"])

        self.assertNotEqual(
            first.definition_version_id, second.definition_version_id
        )
        self.assertEqual(SkillDefinitionVersion.objects.filter(skill=self.skill).count(), 2)

    def test_definition_versions_endpoint_lists_them_newest_first(self):
        with patch("apps.skill.services.generate_chat_completion") as mock_completion, \
             patch("apps.skill.services.fetch_relevant_chunks") as mock_fetch:
            mock_fetch.return_value = [
                SimpleNamespace(document=self.doc, chunk_index=0, content="x")
            ]
            mock_completion.return_value = ("Resultado", {"total_tokens": 5})
            self._run()
            self.skill.system_prompt = "Otra instrucción."
            self.skill.save(update_fields=["system_prompt"])
            self._run()

        response = self.client.get(
            reverse("skill-definition-versions", kwargs={"slug": self.skill.slug})
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        numbers = [row["version_number"] for row in response.data]
        self.assertEqual(numbers, [2, 1])
        self.assertEqual(response.data[0]["executions_count"], 1)


class RerunAndCompareAPITestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="consultor@example.com", password="secret123", username="consultor",
        )
        self.other_user = User.objects.create_user(
            email="otro@example.com", password="secret123", username="otro",
        )
        self.client.force_authenticate(self.user)

        self.project = Project.objects.create(owner=self.user, name="Operación 40")
        self.doc = Document.objects.create(owner=self.user, name="IDO", slug="ido-40")
        ProjectDocument.objects.create(
            project=self.project, document=self.doc, added_by=self.user
        )
        self.skill = Skill.objects.create(
            owner=self.user,
            name="Resumen ejecutivo",
            skill_type=SkillType.QUICK,
            allowed_contexts=["project"],
            system_prompt="Sé preciso.",
            prompt_template="{{context}}\n\n{{extra_instructions}}",
        )

    def _completed_execution(self, content="Resultado uno"):
        with patch("apps.skill.services.generate_chat_completion") as mock_completion, \
             patch("apps.skill.services.fetch_relevant_chunks") as mock_fetch:
            mock_fetch.return_value = [
                SimpleNamespace(document=self.doc, chunk_index=0, content="x")
            ]
            mock_completion.return_value = (content, {"total_tokens": 5})
            execution = SkillExecution.objects.create(
                skill=self.skill, owner=self.user, project=self.project,
            )
            return _run_synchronously(execution)

    def test_rerun_creates_a_second_execution_with_the_same_input(self):
        original = self._completed_execution()
        original.extra_instructions = "Enfatizá el riesgo climático."
        original.save(update_fields=["extra_instructions"])

        with patch("apps.skill.services.generate_chat_completion") as mock_completion, \
             patch("apps.skill.services.fetch_relevant_chunks") as mock_fetch:
            mock_fetch.return_value = [
                SimpleNamespace(document=self.doc, chunk_index=0, content="x")
            ]
            mock_completion.return_value = ("Resultado dos", {"total_tokens": 5})
            response = self.client.post(
                reverse("skill-execution-rerun", kwargs={"pk": original.pk}),
                {"review_each_step": False},
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        new_execution = SkillExecution.objects.get(pk=response.data["id"])
        self.assertNotEqual(new_execution.id, original.id)
        self.assertEqual(new_execution.extra_instructions, original.extra_instructions)
        self.assertEqual(new_execution.project_id, original.project_id)
        self.assertEqual(new_execution.metadata["rerun_of"], original.id)

    def test_rerun_is_not_found_for_a_user_with_no_access_at_all(self):
        original = self._completed_execution()
        self.client.force_authenticate(self.other_user)

        response = self.client.post(
            reverse("skill-execution-rerun", kwargs={"pk": original.pk}), {}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_rerun_is_forbidden_for_a_viewer_without_mutate_access(self):
        """Con acceso de lectura al proyecto alcanza para verla, no para
        repetirla: repetir gasta una corrida entera de modelo."""
        original = self._completed_execution()
        ProjectShare.objects.create(
            project=self.project, user=self.other_user, role=ProjectShareRole.VIEWER
        )
        self.client.force_authenticate(self.other_user)

        response = self.client.post(
            reverse("skill-execution-rerun", kwargs={"pk": original.pk}), {}, format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_compare_without_against_param_is_a_bad_request(self):
        execution = self._completed_execution()
        response = self.client.get(
            reverse("skill-execution-compare", kwargs={"pk": execution.pk})
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_compare_two_runs_with_the_same_definition_and_input_is_comparable(self):
        first = self._completed_execution(content="Mismo contenido")
        second = self._completed_execution(content="Mismo contenido")

        response = self.client.get(
            reverse("skill-execution-compare", kwargs={"pk": first.pk}),
            {"against": second.pk},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["comparable"])
        self.assertEqual(response.data["input"]["definition"]["equal"], True)

    def test_compare_denies_a_run_the_user_cannot_view(self):
        mine = self._completed_execution()
        other_project = Project.objects.create(owner=self.other_user, name="Otra operación")
        theirs = SkillExecution.objects.create(
            skill=self.skill, owner=self.other_user, project=other_project,
            status="completed",
        )

        response = self.client.get(
            reverse("skill-execution-compare", kwargs={"pk": mine.pk}),
            {"against": theirs.pk},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_compare_against_a_nonexistent_execution_is_not_found(self):
        mine = self._completed_execution()
        response = self.client.get(
            reverse("skill-execution-compare", kwargs={"pk": mine.pk}), {"against": 999999},
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
