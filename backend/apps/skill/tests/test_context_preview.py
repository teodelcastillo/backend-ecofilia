"""
El presupuesto de contexto calculado sin correr el workflow.

Lo que se protege acá es que el preview **no mienta**: si dice que un documento
viaja entero, la corrida tiene que verlo entero; si no midió la parte variable,
tiene que decir que no la midió en vez de reportar cero. Un panel que informa un
presupuesto equivocado es peor que no tener panel — el autor decide en base a él.
"""
from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.document.models import Document
from apps.project.models import Project, ProjectDocument
from apps.skill import context_budget
from apps.skill.context_preview import build_preview
from apps.skill.models import (
    Skill,
    SkillStep,
    SkillTier,
    SkillType,
    StepEvidenceMode,
)

User = get_user_model()


def paged(text: str) -> str:
    return f"<<<PAGE:1>>>\n\n{text}"


class ContextPreviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="autor@example.com", password="secret123", username="autor",
        )
        self.project = Project.objects.create(owner=self.user, name="Operación 34")
        self.ido = Document.objects.create(
            owner=self.user, name="IDO", slug="ido-34",
            extracted_text=paged("Documento de la operación. " * 200),
        )
        self.ndc = Document.objects.create(
            owner=self.user, name="NDC", slug="ndc-34",
            extracted_text=paged("Contribución determinada. " * 200),
        )
        for doc in (self.ido, self.ndc):
            ProjectDocument.objects.create(
                project=self.project, document=doc, added_by=self.user
            )
        self.project.blueprint_document = self.ido
        self.project.save(update_fields=["blueprint_document"])

        # Sin dueño, como las plantillas que provee Ecofilia: el preview tiene
        # que resolver el dueño desde la operación.
        self.skill = Skill.objects.create(
            owner=None,
            name="IET",
            skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
            system_prompt="Sé preciso.",
            tier=SkillTier.DEEP,
        )
        SkillStep.objects.create(
            skill=self.skill, title="Marco normativo",
            instructions="Describí el marco.", position=1,
        )
        SkillStep.objects.create(
            skill=self.skill, title="Determinación",
            instructions="Integrá los criterios anteriores.", position=2,
            evidence_mode=StepEvidenceMode.PREVIOUS,
        )

    def test_documents_travel_whole_when_the_corpus_fits(self):
        preview = build_preview(self.skill, self.project)
        step = preview.steps[0]

        self.assertTrue(step.reads_documents)
        self.assertEqual(
            sorted(d.mode for d in step.documents),
            [context_budget.FULL, context_budget.FULL],
        )
        self.assertFalse(step.exceeds_window)

    def test_a_step_that_only_reads_previous_steps_gets_no_documents(self):
        """El runner ni siquiera arma el corpus para estos pasos. Contarles una
        base documental inflaría su presupuesto y mostraría en el panel una
        evidencia que en la corrida no va a existir."""
        preview = build_preview(self.skill, self.project)
        integrador = preview.steps[1]

        self.assertFalse(integrador.reads_documents)
        self.assertEqual(integrador.documents, [])
        self.assertEqual(integrador.cacheable_tokens, 0)
        self.assertEqual(integrador.total_tokens, integrador.reserved_tokens)

    def test_unmeasured_fragments_are_null_not_zero(self):
        """«No lo sabemos» y «no ocupa nada» son respuestas distintas."""
        preview = build_preview(self.skill, self.project, measure_fragments=False)

        self.assertFalse(preview.fragments_measured)
        self.assertIsNone(preview.steps[0].variable_tokens)

    def test_the_blueprint_is_identified(self):
        preview = build_preview(self.skill, self.project)
        blueprint = [d for d in preview.steps[0].documents if d.is_blueprint]

        self.assertEqual([d.slug for d in blueprint], ["ido-34"])
        self.assertEqual(preview.project["blueprint"]["slug"], "ido-34")

    def test_the_step_reports_the_model_its_tier_resolves_to(self):
        """El tier es el control primario; el panel tiene que mostrar en qué
        modelo se traduce, no el id guardado en la skill."""
        with patch.dict("os.environ", {"LLM_PROVIDER": "anthropic"}):
            preview = build_preview(self.skill, self.project)

        self.assertEqual(preview.steps[0].tier, SkillTier.DEEP)
        self.assertEqual(preview.steps[0].tier_source, "skill")
        self.assertTrue(preview.steps[0].model.startswith("claude"))

    def test_a_step_tier_overrides_the_workflow_tier(self):
        step = self.skill.steps.get(position=1)
        step.tier = SkillTier.FAST
        step.save(update_fields=["tier"])

        preview = build_preview(self.skill, self.project)
        self.assertEqual(preview.steps[0].tier, SkillTier.FAST)
        self.assertEqual(preview.steps[0].tier_source, "step")

    def test_an_oversized_document_degrades_and_is_reported(self):
        gigante = Document.objects.create(
            owner=self.user, name="NC4", slug="nc4-34",
            extracted_text=paged("x" * 4_000_000),
        )
        ProjectDocument.objects.create(
            project=self.project, document=gigante, added_by=self.user
        )

        preview = build_preview(self.skill, self.project)
        modes = {d.slug: d.mode for d in preview.steps[0].documents}

        self.assertEqual(modes["nc4-34"], context_budget.PARTIAL)
        # El principal nunca es candidato a degradarse.
        self.assertEqual(modes["ido-34"], context_budget.FULL)

    def test_preview_writes_nothing(self):
        """La ejecución que usa para simular no se guarda: el preview no puede
        aparecer en el historial de corridas de la operación."""
        from apps.skill.models import SkillExecution

        build_preview(self.skill, self.project)
        self.assertEqual(SkillExecution.objects.count(), 0)

    def test_a_workflow_without_steps_is_a_clear_error(self):
        vacia = Skill.objects.create(
            owner=self.user, name="Vacía", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
        )
        with self.assertRaises(ValueError):
            build_preview(vacia, self.project)


class ContextPreviewAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="autor@example.com", password="secret123", username="autor",
        )
        self.otro = User.objects.create_user(
            email="otro@example.com", password="secret123", username="otro",
        )
        self.client.force_authenticate(self.user)

        self.project = Project.objects.create(owner=self.user, name="Operación 34")
        doc = Document.objects.create(
            owner=self.user, name="IDO", slug="ido-api",
            extracted_text=paged("Documento. " * 100),
        )
        ProjectDocument.objects.create(
            project=self.project, document=doc, added_by=self.user
        )
        self.skill = Skill.objects.create(
            owner=self.user, name="IET", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"], system_prompt="Sé preciso.",
        )
        SkillStep.objects.create(
            skill=self.skill, title="Marco", instructions="Describí.", position=1,
        )

    def _url(self):
        return reverse("skill-context-preview", kwargs={"slug": self.skill.slug})

    def test_returns_the_budget_per_step(self):
        response = self.client.get(self._url(), {"project": self.project.slug})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["schema"], 1)
        self.assertEqual(len(response.data["steps"]), 1)
        self.assertIn("cacheable_tokens", response.data["steps"][0])
        self.assertFalse(response.data["fragments_measured"])
        self.assertIsNone(response.data["steps"][0]["variable_tokens"])

    def test_missing_project_param_is_a_bad_request(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_project_the_user_cannot_see_is_not_found(self):
        ajena = Project.objects.create(owner=self.otro, name="Ajena")
        response = self.client.get(self._url(), {"project": ajena.slug})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_a_workflow_without_steps_is_a_bad_request_not_a_crash(self):
        vacia = Skill.objects.create(
            owner=self.user, name="Vacía", skill_type=SkillType.COPILOT,
            allowed_contexts=["project"],
        )
        response = self.client.get(
            reverse("skill-context-preview", kwargs={"slug": vacia.slug}),
            {"project": self.project.slug},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
