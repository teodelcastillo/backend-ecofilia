"""
Bloque de contexto de la operación inyectado en las ejecuciones de skills.

`build_operation_context_block` no toca la base ni Django: recibe cualquier
objeto con la forma de un Project, así que se prueba con dobles y sin
fixtures.
"""
from types import SimpleNamespace

from django.test import SimpleTestCase

from apps.skill.operation_context import build_operation_context_block


def _document(**overrides):
    base = dict(
        name="IDO Préstamo — Argentina",
        description="Informe de originación",
        content_summary="Préstamo A/B de hasta USD 400 millones para actividades de gas.",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _project(**overrides):
    base = dict(
        name="Pan American Energy SL",
        description="",
        blueprint_document=_document(),
        context_notes={
            "pais": "Argentina",
            "sector": "Energía",
            "monto": "400",
            "estado": "originacion",
            "objetivo": "Financiar el plan de inversiones en gas natural.",
            "componentes": "1. Upstream no convencional\n2. Infraestructura de GNL",
        },
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class BuildOperationContextBlockTests(SimpleTestCase):
    def test_includes_operation_data_loaded_by_the_user(self):
        block = build_operation_context_block(_project())

        self.assertIn("Pan American Energy SL", block)
        self.assertIn("País: Argentina", block)
        self.assertIn("Sector: Energía", block)
        self.assertIn("Financiar el plan de inversiones en gas natural.", block)
        self.assertIn("Upstream no convencional", block)

    def test_includes_the_blueprint_document(self):
        block = build_operation_context_block(_project())

        self.assertIn("IDO Préstamo — Argentina", block)
        self.assertIn("USD 400 millones", block)

    def test_estado_is_rendered_as_a_label_not_a_slug(self):
        block = build_operation_context_block(_project())

        self.assertIn("En originación", block)
        self.assertNotIn("Estado de avance: originacion", block)

    def test_unknown_context_note_keys_still_reach_the_model(self):
        """Un campo nuevo del formulario no debe quedar invisible."""
        project = _project(context_notes={"pais": "Chile", "campo_nuevo": "un valor"})

        block = build_operation_context_block(project)

        self.assertIn("un valor", block)

    def test_tramitacion_fields_are_left_out(self):
        """Identificadores internos son ruido para el análisis técnico."""
        project = _project(
            context_notes={"pais": "Chile", "link_focus": "https://focus/x"}
        )

        block = build_operation_context_block(project)

        self.assertNotIn("focus/x", block)

    def test_long_summaries_are_truncated(self):
        project = _project(blueprint_document=_document(content_summary="x" * 9000))

        block = build_operation_context_block(project)

        self.assertLess(len(block), 6000)
        self.assertIn("…", block)

    def test_missing_blueprint_is_stated_rather_than_omitted(self):
        project = _project(blueprint_document=None)

        block = build_operation_context_block(project)

        self.assertIn("no tiene un documento marcado como principal", block)

    def test_no_project_yields_no_block(self):
        """Ejecuciones sobre repositorio o documento suelto no llevan contexto."""
        self.assertEqual(build_operation_context_block(None), "")

    def test_always_demands_grounding_in_the_operation_documents(self):
        block = build_operation_context_block(_project())

        self.assertIn("No completes con conocimiento general", block)
