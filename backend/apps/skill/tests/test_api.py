from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase


User = get_user_model()


class SkillAPITestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="owner@example.com",
            password="secret123",
            username="owner",
        )
        self.client.force_authenticate(self.user)

    def test_create_skill_sets_hybrid_retrieval_for_comparative_mode(self):
        payload = {
            "name": "Comparative Goals",
            "description": "Compare goals by document",
            "skill_type": "quick",
            "allowed_contexts": ["project"],
            "system_prompt": "Use evidence only.",
            "prompt_template": "Analyze docs\n\n{{context}}",
            "model": "gpt-4o-mini",
            "temperature": 0.2,
            "comparative_mode_enabled": True,
            "strict_missing_evidence": True,
            "retrieval_strategy": "global",
            "k_per_doc": 2,
            "total_limit": 10,
            "max_per_doc_after_rerank": 3,
        }
        response = self.client.post(reverse("skill-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["retrieval_strategy"], "hybrid_per_document")
        self.assertTrue(response.data["comparative_mode_enabled"])

    def test_create_quick_table_skill_persists_schema(self):
        payload = {
            "name": "Compliance Matrix",
            "description": "Generate a compliance table",
            "skill_type": "quick",
            "allowed_contexts": ["project"],
            "system_prompt": "Use evidence only.",
            "prompt_template": "Tabular analysis\n\n{{context}}",
            "model": "gpt-4o-mini",
            "temperature": 0.2,
            "default_output_mode": "table",
            "table_schema": {
                "name": "Compliance",
                "description": "Per-document compliance",
                "columns": [
                    {
                        "key": "titulo",
                        "label": "Titulo",
                        "type": "text",
                        "required": True,
                    },
                    {
                        "key": "anio",
                        "label": "Anio",
                        "type": "number",
                        "required": True,
                    },
                    {
                        "key": "cumple",
                        "label": "Cumple",
                        "type": "boolean",
                        "required": True,
                        "prompt_hint": "true si hay evidencia explicita.",
                    },
                ],
            },
        }
        response = self.client.post(reverse("skill-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["default_output_mode"], "table")
        columns = response.data["table_schema"]["columns"]
        self.assertEqual([c["key"] for c in columns], ["titulo", "anio", "cumple"])
        self.assertEqual(columns[2]["prompt_hint"], "true si hay evidencia explicita.")

    def test_create_quick_table_skill_rejects_empty_schema(self):
        payload = {
            "name": "Empty Schema",
            "skill_type": "quick",
            "allowed_contexts": ["project"],
            "system_prompt": "Use evidence only.",
            "prompt_template": "{{context}}",
            "model": "gpt-4o-mini",
            "temperature": 0.2,
            "default_output_mode": "table",
            "table_schema": {"columns": []},
        }
        response = self.client.post(reverse("skill-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("table_schema", response.data)

    def test_create_quick_skill_rejects_schema_when_text_mode(self):
        payload = {
            "name": "Text Mode With Schema",
            "skill_type": "quick",
            "allowed_contexts": ["project"],
            "system_prompt": "Use evidence only.",
            "prompt_template": "{{context}}",
            "model": "gpt-4o-mini",
            "temperature": 0.2,
            "default_output_mode": "text",
            "table_schema": {
                "columns": [
                    {"key": "x", "label": "X", "type": "text"},
                ]
            },
        }
        response = self.client.post(reverse("skill-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("table_schema", response.data)

    def test_create_copilot_with_tabular_step(self):
        payload = {
            "name": "Mixed Copilot",
            "skill_type": "copilot",
            "allowed_contexts": ["project"],
            "system_prompt": "Use evidence only.",
            "prompt_template": "",
            "model": "gpt-4o-mini",
            "temperature": 0.3,
            "steps": [
                {
                    "title": "Resumen",
                    "instructions": "Escribe un resumen.",
                    "position": 1,
                },
                {
                    "title": "Matriz",
                    "instructions": "Construye una matriz.",
                    "position": 2,
                    "output_mode": "table",
                    "table_schema": {
                        "columns": [
                            {"key": "criterio", "label": "Criterio", "type": "text"},
                            {"key": "cumple", "label": "Cumple", "type": "boolean"},
                        ]
                    },
                },
            ],
        }
        response = self.client.post(reverse("skill-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        steps = response.data["steps"]
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["output_mode"], "text")
        self.assertEqual(steps[1]["output_mode"], "table")
        keys = [c["key"] for c in steps[1]["table_schema"]["columns"]]
        self.assertEqual(keys, ["criterio", "cumple"])

    def test_create_copilot_table_step_requires_schema(self):
        payload = {
            "name": "Bad Copilot",
            "skill_type": "copilot",
            "allowed_contexts": ["project"],
            "system_prompt": "Use evidence only.",
            "prompt_template": "",
            "model": "gpt-4o-mini",
            "temperature": 0.3,
            "steps": [
                {
                    "title": "Matriz",
                    "instructions": "Construye una matriz.",
                    "position": 1,
                    "output_mode": "table",
                    "table_schema": {"columns": []},
                },
            ],
        }
        response = self.client.post(reverse("skill-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_copilot_rejects_default_table(self):
        payload = {
            "name": "Bad Copilot",
            "skill_type": "copilot",
            "allowed_contexts": ["project"],
            "system_prompt": "Use evidence only.",
            "prompt_template": "",
            "model": "gpt-4o-mini",
            "temperature": 0.3,
            "default_output_mode": "table",
            "steps": [
                {
                    "title": "Resumen",
                    "instructions": "Escribe un resumen.",
                    "position": 1,
                },
            ],
        }
        response = self.client.post(reverse("skill-list"), payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("default_output_mode", response.data)


class WorkflowEvidenceTagsAPITestCase(APITestCase):
    """El contrato de evidencia, tal como lo escribe el builder."""

    def setUp(self):
        self.user = User.objects.create_user(
            email="autor@example.com", password="secret123", username="autor"
        )
        self.client.force_authenticate(self.user)

    def _payload(self, step_overrides: dict) -> dict:
        step = {
            "title": "CT M3 — Consistencia con la NDC",
            "instructions": "Analizá la consistencia.",
            "position": 1,
        }
        step.update(step_overrides)
        return {
            "name": "Workflow con etiquetas",
            "description": "",
            "skill_type": "copilot",
            "allowed_contexts": ["project"],
            "system_prompt": "Sos un analista.",
            "steps": [step],
        }

    def test_step_can_declare_evidence_tags(self):
        response = self.client.post(
            reverse("skill-list"),
            self._payload({"evidence_selection": "tagged", "evidence_tags": ["ndc"]}),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        step = response.data["steps"][0]
        self.assertEqual(step["evidence_selection"], "tagged")
        self.assertEqual(step["evidence_tags"], ["ndc"])

    def test_unknown_tag_is_rejected(self):
        """Un slug inventado no rompe la corrida — y por eso hay que frenarlo acá.

        El paso simplemente no encontraría documentos, y el autor se enteraría
        recién al leer una sección vacía.
        """
        response = self.client.post(
            reverse("skill-list"),
            self._payload(
                {"evidence_selection": "tagged", "evidence_tags": ["no-existe"]}
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_tagged_without_tags_or_documents_is_rejected(self):
        response = self.client.post(
            reverse("skill-list"),
            self._payload({"evidence_selection": "tagged", "evidence_tags": []}),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_default_selection_reads_everything(self):
        """Los workflows que ya existían no cambian de comportamiento."""
        response = self.client.post(
            reverse("skill-list"), self._payload({}), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["steps"][0]["evidence_selection"], "all")
