"""
Comparar dos corridas: primero si son comparables, después en qué difieren.

El orden importa: dos corridas con distinto input pueden diferir en la salida
por razones legítimas, y tratar eso como falta de determinismo es exactamente
el error que este módulo existe para evitar. Por eso cada test de "comparable"
verifica también que ``input_differences``/``input_unknown`` señalen la causa.

Trabaja sobre objetos de prueba con la forma de ``SkillExecution`` — sin base,
igual que ``context_budget`` y ``definition``.
"""
from dataclasses import dataclass, field
from datetime import datetime

from django.test import SimpleTestCase

from apps.skill import comparison as cmp


@dataclass
class FakeVersion:
    version_number: int
    fingerprint: str
    definition: dict


@dataclass
class FakeExecution:
    id: int
    metadata: dict = field(default_factory=dict)
    document_snapshot: list = field(default_factory=list)
    output_structured: dict = field(default_factory=dict)
    input_values: dict = field(default_factory=dict)
    extra_instructions: str = ""
    definition_version: object = None
    status: str = "completed"
    created_at: object = None


def manifest(**overrides) -> dict:
    base = {
        "provider": "anthropic",
        "models_used": ["claude-opus-5"],
        "retrieval": {"context_first": True},
        "scope": {"document_slugs_filter": []},
    }
    base.update(overrides)
    return {"run_manifest": base}


def doc(slug, chunk_count=10, last_chunk_id=999):
    return {"slug": slug, "chunk_count": chunk_count, "last_chunk_id": last_chunk_id}


def make_execution(execution_id=1, *, version=None, **kwargs) -> FakeExecution:
    return FakeExecution(id=execution_id, definition_version=version, **kwargs)


SAME_DEF = FakeVersion(1, "sha256:same", {"skill": {}, "steps": [], "parameters": []})
OTHER_DEF = FakeVersion(2, "sha256:other", {"skill": {"tier": "deep"}, "steps": [], "parameters": []})


class ComparabilityTests(SimpleTestCase):
    def test_same_input_on_every_axis_is_comparable(self):
        a = make_execution(1, version=SAME_DEF, metadata=manifest(), document_snapshot=[doc("ido")])
        b = make_execution(2, version=SAME_DEF, metadata=manifest(), document_snapshot=[doc("ido")])

        report = cmp.compare_executions(a, b)
        self.assertTrue(report["comparable"])
        self.assertEqual(report["input_differences"], [])

    def test_different_definition_is_not_comparable_and_says_so(self):
        a = make_execution(1, version=SAME_DEF, metadata=manifest())
        b = make_execution(2, version=OTHER_DEF, metadata=manifest())

        report = cmp.compare_executions(a, b)
        self.assertFalse(report["comparable"])
        self.assertIn("la definición del workflow", report["input_differences"])

    def test_missing_definition_version_is_unknown_not_equal(self):
        """Sin versión registrada no se puede afirmar que el input fue el
        mismo: declararlas iguales sería el peor resultado posible de una
        herramienta de auditoría."""
        a = make_execution(1, version=None, metadata=manifest())
        b = make_execution(2, version=SAME_DEF, metadata=manifest())

        report = cmp.compare_executions(a, b)
        self.assertIsNone(report["comparable"])
        self.assertIn("la definición del workflow", report["input_unknown"])
        self.assertNotIn("la definición del workflow", report["input_differences"])

    def test_reprocessed_document_is_flagged_even_with_the_same_slug(self):
        a = make_execution(
            1, version=SAME_DEF, metadata=manifest(),
            document_snapshot=[doc("ndc", chunk_count=40, last_chunk_id=100)],
        )
        b = make_execution(
            2, version=SAME_DEF, metadata=manifest(),
            document_snapshot=[doc("ndc", chunk_count=41, last_chunk_id=140)],
        )

        report = cmp.compare_executions(a, b)
        self.assertFalse(report["comparable"])
        self.assertEqual(report["input"]["documents"]["reprocessed"][0]["slug"], "ndc")

    def test_different_retrieval_config_is_flagged(self):
        a = make_execution(1, version=SAME_DEF, metadata=manifest(retrieval={"context_first": True}))
        b = make_execution(2, version=SAME_DEF, metadata=manifest(retrieval={"context_first": False}))

        report = cmp.compare_executions(a, b)
        self.assertFalse(report["comparable"])
        self.assertIn("la configuración de recuperación", report["input_differences"])

    def test_different_provider_is_flagged(self):
        a = make_execution(1, version=SAME_DEF, metadata=manifest(provider="anthropic"))
        b = make_execution(2, version=SAME_DEF, metadata=manifest(provider="openai"))

        report = cmp.compare_executions(a, b)
        self.assertIn("el proveedor o los modelos usados", report["input_differences"])


class OutputComparisonTests(SimpleTestCase):
    def test_identical_content_and_references_report_no_change(self):
        step = {
            "title": "Marco normativo",
            "content": "El país adhiere al Acuerdo de París.",
            "citations": [{"document_slug": "ndc", "reference": "p. 4"}],
        }
        a = make_execution(1, version=SAME_DEF, output_structured={"steps": [step]})
        b = make_execution(2, version=SAME_DEF, output_structured={"steps": [dict(step)]})

        report = cmp.compare_executions(a, b)
        self.assertEqual(report["output"]["steps"][0]["status"], "identical")
        self.assertEqual(report["output"]["steps_with_same_references"], 1)

    def test_different_citations_are_reported_even_if_prose_is_similar(self):
        """Es el corazón de la trazabilidad: la redacción puede variar, pero
        que cite otra página para lo mismo tiene que verse."""
        step_a = {
            "title": "Riesgo climático",
            "content": "El proyecto está expuesto a inundaciones.",
            "citations": [{"document_slug": "ido", "reference": "p. 12"}],
        }
        step_b = {
            "title": "Riesgo climático",
            "content": "El proyecto está expuesto a inundaciones.",
            "citations": [{"document_slug": "ido", "reference": "p. 47"}],
        }
        a = make_execution(1, version=SAME_DEF, output_structured={"steps": [step_a]})
        b = make_execution(2, version=SAME_DEF, output_structured={"steps": [step_b]})

        report = cmp.compare_executions(a, b)
        entry = report["output"]["steps"][0]
        self.assertEqual(entry["status"], "changed")
        self.assertFalse(entry["references"]["equal"])
        self.assertEqual(entry["references"]["only_in_a"], ["[ido] p. 12"])
        self.assertEqual(entry["references"]["only_in_b"], ["[ido] p. 47"])

    def test_table_cell_change_is_reported_by_row_and_column(self):
        step_a = {
            "title": "Determinación",
            "output_mode": "table",
            "table": {"rows": [{"criterio": "Adaptación", "resultado": "Alineado"}]},
        }
        step_b = {
            "title": "Determinación",
            "output_mode": "table",
            "table": {"rows": [{"criterio": "Adaptación", "resultado": "No alineado"}]},
        }
        a = make_execution(1, version=SAME_DEF, output_structured={"steps": [step_a]})
        b = make_execution(2, version=SAME_DEF, output_structured={"steps": [step_b]})

        report = cmp.compare_executions(a, b)
        entry = report["output"]["steps"][0]
        self.assertEqual(entry["status"], "changed")
        self.assertEqual(
            entry["table"]["cells_changed"],
            [{"row": 0, "column": "resultado", "a": "Alineado", "b": "No alineado"}],
        )

    def test_extra_step_in_one_run_does_not_crash_the_comparison(self):
        step = {"title": "Único", "content": "x"}
        a = make_execution(1, version=SAME_DEF, output_structured={"steps": [step, step]})
        b = make_execution(2, version=SAME_DEF, output_structured={"steps": [step]})

        report = cmp.compare_executions(a, b)
        self.assertEqual(report["output"]["steps"][-1]["status"], "only_in_a")
