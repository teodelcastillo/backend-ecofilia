"""
Las instrucciones que el motor inyecta en cada paso, según la situación.

La era del RAG dejó instrucciones que eran correctas para su momento y dejan de
serlo con el expediente completo delante. La poda no es borrarlas —el camino de
recuperación sigue existiendo como plan B y ahí siguen siendo correctas— sino
que el motor elija cuál corresponde.
"""
from django.test import SimpleTestCase

from apps.skill.services import (
    COPILOT_DELIVERABLE_STANDARD,
    COPILOT_DELIVERABLE_STANDARD_CONTEXT_FIRST,
    _comparative_instruction_block,
    deliverable_standard,
    step_output_rules,
)


class DeliverableStandardTests(SimpleTestCase):
    def test_retrieval_path_still_asks_for_depth(self):
        """Con seis mil tokens de fragmentos el riesgo es una sección flaca.
        Pedir profundidad ahí sigue siendo lo correcto."""
        standard = deliverable_standard(context_first=False)

        self.assertEqual(standard, COPILOT_DELIVERABLE_STANDARD)
        self.assertIn("profundidad", standard)

    def test_context_first_asks_for_selection_instead(self):
        """Con el expediente completo el riesgo se invierte: sobra material y
        la tentación es resumirlo todo."""
        standard = deliverable_standard(context_first=True)

        self.assertEqual(standard, COPILOT_DELIVERABLE_STANDARD_CONTEXT_FIRST)
        self.assertNotIn("profundidad", standard)
        self.assertIn("dejar afuera", standard)

    def test_both_keep_the_audience(self):
        """Para quién se escribe es lo único que el modelo no puede deducir del
        material, así que no se poda."""
        for context_first in (True, False):
            with self.subTest(context_first=context_first):
                self.assertIn(
                    "banca de desarrollo", deliverable_standard(context_first=context_first)
                )

    def test_context_first_does_not_repeat_the_inventory_rules(self):
        """El inventario dice qué se puede afirmar y qué se puede citar, con
        más precisión. Repetirlo acá sólo lo diluye."""
        standard = deliverable_standard(context_first=True)

        self.assertNotIn("No inventes", standard)
        self.assertNotIn("insuficiente", standard)


class ComparativeBlockTests(SimpleTestCase):
    def test_without_inventory_it_forces_coverage(self):
        """Para eso nació: sin él la recuperación traía tres de seis documentos
        y la sección hablaba de tres."""
        block = _comparative_instruction_block(True, has_inventory=False)

        self.assertIn("incluí todos los documentos activos", block)
        self.assertIn("Sin evidencia en fuentes provistas", block)

    def test_with_inventory_it_drops_the_single_formula(self):
        """La fórmula única aplasta los tres estados que el inventario acaba de
        distinguir: una ausencia real no es un documento mencionado que no
        tenemos."""
        block = _comparative_instruction_block(True, has_inventory=True)

        self.assertNotIn("Sin evidencia en fuentes provistas", block)
        self.assertIn("tres del inventario", block)

    def test_coverage_is_still_required_with_inventory(self):
        """Deja de forzarse contra la recuperación, no deja de pedirse: una
        tabla comparativa con un documento ausente sigue estando mal."""
        block = _comparative_instruction_block(False, has_inventory=True)

        self.assertIn("Cubrí cada documento del inventario", block)

    def test_strictness_flag_is_moot_once_there_is_an_inventory(self):
        """El flag elegía entre dos redacciones de la misma fórmula única; con
        inventario ninguna de las dos aplica."""
        strict = _comparative_instruction_block(True, has_inventory=True)
        loose = _comparative_instruction_block(False, has_inventory=True)

        self.assertEqual(strict, loose)


class StepOutputRulesTests(SimpleTestCase):
    """Las reglas de forma de un paso las decide el motor, una por decisión.

    El caso real: pasos que pedían «un solo párrafo» salían con cinco, y
    comparaciones de alineación salían con un subtítulo por documento. El
    estándar del motor viajaba siempre detrás de la instrucción del paso y el
    modelo elegía cuál pesaba más.
    """

    def _rules(self, step="", workflow="", **overrides):
        options = {
            "step_format": step,
            "workflow_format": workflow,
            "is_table_step": False,
            "comparative": False,
            "strict_missing_evidence": True,
            "context_first": True,
        }
        options.update(overrides)
        return "\n".join(step_output_rules(**options))

    def test_step_format_replaces_the_engine_standard(self):
        rules = self._rules("Un único párrafo, sin subtítulos ni listas.")

        self.assertIn("Un único párrafo, sin subtítulos ni listas.", rules)
        self.assertNotIn("subtítulos, párrafos y listas", rules)

    def test_step_format_wins_over_the_workflow_format(self):
        rules = self._rules("Un párrafo.", "Tres párrafos con subtítulos.")

        self.assertIn("Un párrafo.", rules)
        self.assertNotIn("Tres párrafos", rules)

    def test_workflow_format_applies_when_the_step_has_none(self):
        rules = self._rules("", "Tres párrafos con subtítulos.")

        self.assertIn("Tres párrafos con subtítulos.", rules)
        self.assertNotIn("subtítulos, párrafos y listas", rules)

    def test_engine_standard_is_the_fallback(self):
        rules = self._rules("", "")

        self.assertEqual(rules, COPILOT_DELIVERABLE_STANDARD_CONTEXT_FIRST)

    def test_blank_format_counts_as_absent(self):
        self.assertEqual(self._rules("   \n ", " "), self._rules("", ""))

    def test_comparative_is_independent_of_the_format(self):
        """Apagar el comparativo no toca el formato, y declarar un formato no
        apaga el comparativo: son dos selectores distintos."""
        with_both = self._rules("Un párrafo.", comparative=True)
        self.assertIn("Requisitos comparativos", with_both)
        self.assertIn("Un párrafo.", with_both)

        without = self._rules("Un párrafo.", comparative=False)
        self.assertNotIn("Requisitos comparativos", without)
        self.assertIn("Un párrafo.", without)

    def test_table_steps_get_no_prose_rules(self):
        """Su forma es el esquema JSON; un formato de prosa lo contradiría."""
        self.assertEqual(self._rules("Un párrafo.", "X", is_table_step=True, comparative=True), "")
