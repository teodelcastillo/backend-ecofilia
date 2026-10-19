"""
Las reglas de los dos objetivos y lo que se lee de la IET de Ecofilia.

Los objetivos no se eligen: salen de sus CT con una tabla que comparten el
portal, la síntesis de la IET y este módulo. Acá está esa tabla
caso por caso, y cómo se aplica a la mirada del ejecutivo y a la de Ecofilia.
"""
from django.test import SimpleTestCase, TestCase

from apps.project.services.caf_criteria import (
    build_criterio_ejecutivo,
    determinar_objetivo_1,
    determinar_objetivo_2,
    ecofilia_determinations,
    ecofilia_green_components,
    executive_assessment,
    guided_basis,
)

A1 = "CT A1 — Análisis de riesgos climáticos"
A2 = "CT A2 — Análisis de medidas"
A3 = "CT A3 — Consistencia con los instrumentos nacionales de adaptación"
OBJ_1 = "Objetivo 1 — Desarrollo resiliente al clima"


class DeterminarObjetivo1TestCase(SimpleTestCase):
    # (ct_a1, ct_a2, ct_a3) → Objetivo 1. La tabla del plan, fila por fila.
    CASOS = [
        # Riesgo bajo / No aplica AR: CT A2 se salta y, con consistencia, alineada.
        (("Riesgo bajo", "", "Consistencia"), "Alineada"),
        (("No aplica análisis de riesgos (AR)", "", "Consistencia"), "Alineada"),
        (("Riesgo bajo", "No suficientes", "Consistencia"), "Alineada"),
        # Riesgo medio / alto: depende de las medidas.
        (("Riesgo medio", "Suficientes", "Consistencia"), "Alineada"),
        (("Riesgo alto", "Suficientes", "Consistencia"), "Alineada"),
        (("Riesgo medio", "No suficientes", "Consistencia"), "Con brechas por CT A2"),
        (("Riesgo alto", "Información insuficiente", "Consistencia"), "Con brechas por CT A2"),
        # Sin análisis de riesgos: brecha por CT A1.
        (("Indeterminado", "Suficientes", "Consistencia"), "Con brechas por CT A1"),
        (("Sin análisis de riesgos (AR)", "", "Consistencia"), "Con brechas por CT A1"),
        # CT A3 manda sobre todo lo demás.
        (("Riesgo bajo", "", "Potencialmente inconsistente"), "Con brechas por CT A3"),
        (("Riesgo medio", "No suficientes", "Potencialmente inconsistente"), "Con brechas por CT A3"),
        (("Riesgo medio", "Suficientes", "Inconsistente"), "No alineada"),
        (("Indeterminado", "", "Inconsistente"), "No alineada"),
    ]

    def test_the_table(self):
        for entrada, esperado in self.CASOS:
            valor, motivo = determinar_objetivo_1(*entrada)
            self.assertEqual(valor, esperado, entrada)
            self.assertTrue(motivo, entrada)

    def test_precedence_a2_over_a1(self):
        # Sin AR pero con medidas insuficientes: la brecha que se nombra es la de A2.
        self.assertEqual(
            determinar_objetivo_1("Indeterminado", "No suficientes", "Consistencia")[0],
            "Con brechas por CT A2",
        )

    def test_without_ct_a3_there_is_no_determination(self):
        for a1, a2 in (("Riesgo bajo", ""), ("Riesgo medio", "Suficientes"), ("", "")):
            valor, motivo = determinar_objetivo_1(a1, a2, "")
            self.assertIsNone(valor)
            self.assertIn("CT A3", motivo)

    def test_medium_risk_without_measures_is_pending(self):
        valor, motivo = determinar_objetivo_1("Riesgo medio", "", "Consistencia")
        self.assertIsNone(valor)
        self.assertIn("CT A2", motivo)

    def test_unknown_values_do_not_determine(self):
        self.assertIsNone(determinar_objetivo_1("Riesgo medio", "Suficientes", "Otra cosa")[0])
        self.assertIsNone(determinar_objetivo_1("Otra cosa", "Suficientes", "Consistencia")[0])


class DeterminarObjetivo2TestCase(SimpleTestCase):
    # (m1, m2, m3, m4, m5) → (Objetivo 2, CT que no aplican). El recorrido termina donde un CT decide.
    CASOS = [
        (("Contiene actividades no alineadas", "", "", "", ""), "No alineada", ("ct_m2", "ct_m3", "ct_m4", "ct_m5")),
        (("Sin actividades no alineadas", "Contiene actividades alineadas", "", "", ""), "Alineada en el CT M2", ("ct_m3", "ct_m4", "ct_m5")),
        (("Sin actividades no alineadas", "Sin actividades alineadas", "Consistencia", "", ""), "Alineada en el CT M3", ("ct_m4", "ct_m5")),
        (("Sin actividades no alineadas", "Sin actividades alineadas", "Inconsistente", "", ""), "No alineada", ("ct_m4", "ct_m5")),
        (("Sin actividades no alineadas", "Sin actividades alineadas", "Potencialmente inconsistente", "Inconsistente", ""), "No alineada", ("ct_m5",)),
        (("Sin actividades no alineadas", "Sin actividades alineadas", "Potencialmente inconsistente", "Consistencia", "No persisten riesgos de transición"), "Alineada en el CT M5", ()),
        (("Sin actividades no alineadas", "Sin actividades alineadas", "Potencialmente inconsistente", "Potencialmente inconsistente", "Persisten riesgos de transición"), "Con brechas", ()),
    ]

    def test_the_table(self):
        for entrada, esperado, no_aplican in self.CASOS:
            valor, motivo, saltados = determinar_objetivo_2(*entrada)
            self.assertEqual(valor, esperado, entrada)
            self.assertEqual(saltados, no_aplican, entrada)
            self.assertTrue(motivo, entrada)

    def test_pending_while_the_deciding_ct_is_missing(self):
        self.assertIsNone(determinar_objetivo_2("", "", "", "", "")[0])
        self.assertIsNone(determinar_objetivo_2("Sin actividades no alineadas", "", "", "", "")[0])
        self.assertIsNone(
            determinar_objetivo_2("Sin actividades no alineadas", "Sin actividades alineadas", "Potencialmente inconsistente", "", "")[0]
        )
        self.assertIsNone(
            determinar_objetivo_2("Sin actividades no alineadas", "Sin actividades alineadas", "Potencialmente inconsistente", "Consistencia", "")[0]
        )

    def test_unknown_values_do_not_determine(self):
        self.assertIsNone(determinar_objetivo_2("Otra cosa", "", "", "", "")[0])

    def test_executive_cts_after_the_end_become_no_aplica(self):
        notes = {
            "alineacion_paris": {
                "ct_m1": "Sin actividades no alineadas",
                "ct_m2": "Contiene actividades alineadas",
                "ct_m3": "Consistencia",
                "objetivo_2": "Con brechas",
            }
        }
        det = executive_assessment(notes)["determinaciones"]
        self.assertEqual(det["objetivo_2"], "Alineada en el CT M2")
        self.assertEqual(det["ct_m3"], "No aplica")
        self.assertTrue(executive_assessment(notes)["objetivo_2_motivo"])


class ExecutiveAssessmentTestCase(SimpleTestCase):
    def test_objetivo_1_is_derived_from_the_executive_cts(self):
        notes = {
            "alineacion_paris": {
                "ct_a1": "Riesgo medio", "ct_a2": "No suficientes", "ct_a3": "Consistencia",
                "objetivo_1": "Alineada", "actualizado_en": "2026-10-19",
            },
        }
        assessment = executive_assessment(notes)
        self.assertEqual(assessment["determinaciones"]["objetivo_1"], "Con brechas por CT A2")
        self.assertIn("No suficientes", assessment["objetivo_1_motivo"])
        # Lo que se compara después contra el criterio actual lleva el mismo valor.
        self.assertEqual(guided_basis(notes)["determinaciones"]["objetivo_1"], "Con brechas por CT A2")

    def test_low_risk_skips_ct_a2(self):
        notes = {"alineacion_paris": {"ct_a1": "Riesgo bajo", "ct_a2": "No suficientes", "ct_a3": "Consistencia"}}
        determinaciones = executive_assessment(notes)["determinaciones"]
        self.assertEqual(determinaciones["ct_a2"], "No aplica")
        self.assertEqual(determinaciones["objetivo_1"], "Alineada")

    def test_without_ct_a3_what_the_executive_wrote_stays(self):
        notes = {"alineacion_paris": {"ct_a1": "Riesgo medio", "objetivo_1": "Con brechas por CT A2"}}
        assessment = executive_assessment(notes)
        self.assertEqual(assessment["determinaciones"]["objetivo_1"], "Con brechas por CT A2")
        self.assertEqual(assessment["objetivo_1_motivo"], "")

    def test_the_criterio_text_carries_the_derived_objetivo_1(self):
        notes = {"alineacion_paris": {"ct_a1": "Riesgo alto", "ct_a2": "Suficientes", "ct_a3": "Inconsistente"}}
        criterio = build_criterio_ejecutivo(notes)
        self.assertIn(f"- {OBJ_1}: No alineada.", criterio)


class _Run:
    """Una corrida de IET con sólo lo que leen estas funciones."""

    def __init__(self, steps):
        self.output_structured = {"steps": steps}


def _s1(rows):
    return {"step_id": 9, "title": "S.1. Síntesis por criterio", "content": "", "table": {"rows": rows}}


class EcofiliaDeterminationsTestCase(TestCase):
    def test_objetivo_1_is_recalculated_from_the_synthesis_cts(self):
        run = _Run([_s1([
            {"criterio": A1, "determinacion": "Riesgo medio", "fundamento": "APRC medio.",
             "informacion_faltante": ""},
            {"criterio": A2, "determinacion": "No suficientes", "fundamento": "Sin medidas.",
             "informacion_faltante": "Falta el plan de medidas."},
            {"criterio": A3, "determinacion": "Consistencia", "fundamento": "NAP."},
            {"criterio": OBJ_1, "determinacion": "Alineada", "fundamento": "Prosa optimista."},
        ])])
        out = ecofilia_determinations(run)
        self.assertEqual(out["ct_a2"]["informacion_faltante"], "Falta el plan de medidas.")
        self.assertEqual(out["ct_a1"]["informacion_faltante"], "")
        objetivo = out["objetivo_1"]
        self.assertEqual(objetivo["determinacion"], "Con brechas por CT A2")
        self.assertEqual(objetivo["objetivo_1_texto"], "Alineada")
        self.assertIn("No suficientes", objetivo["fundamento"])

    def test_when_the_prose_agrees_nothing_is_flagged(self):
        run = _Run([_s1([
            {"criterio": A1, "determinacion": "Riesgo bajo", "fundamento": "x"},
            {"criterio": A3, "determinacion": "Consistencia", "fundamento": "x"},
            {"criterio": OBJ_1, "determinacion": "Alineada", "fundamento": "x"},
        ])])
        objetivo = ecofilia_determinations(run)["objetivo_1"]
        self.assertEqual(objetivo["determinacion"], "Alineada")
        self.assertNotIn("objetivo_1_texto", objetivo)

    def test_an_inconsistent_ct_a3_means_not_aligned_even_without_objetivo_row(self):
        run = _Run([_s1([
            {"criterio": A1, "determinacion": "Riesgo medio", "fundamento": "x"},
            {"criterio": A2, "determinacion": "Suficientes", "fundamento": "x"},
            {"criterio": A3, "determinacion": "Inconsistente", "fundamento": "Contradice la NDC."},
        ])])
        self.assertEqual(ecofilia_determinations(run)["objetivo_1"]["determinacion"], "No alineada")

    def test_without_ct_a3_the_prose_stays(self):
        run = _Run([_s1([
            {"criterio": A1, "determinacion": "Riesgo bajo", "fundamento": "x"},
            {"criterio": OBJ_1, "determinacion": "Con brechas por CT A2", "fundamento": "Faltan medidas."},
        ])])
        self.assertEqual(
            ecofilia_determinations(run)["objetivo_1"],
            {"determinacion": "Con brechas por CT A2", "fundamento": "Faltan medidas.",
             "informacion_faltante": ""},
        )

    def test_no_synthesis_no_determinations(self):
        self.assertEqual(ecofilia_determinations(None), {})
        self.assertEqual(ecofilia_determinations(_Run([{"title": "B.1. Marco", "content": "x"}])), {})


class EcofiliaGreenComponentsTestCase(TestCase):
    def test_the_components_come_from_s2_without_cell_citations(self):
        run = _Run([
            {"step_id": 1, "title": "C.1. Actividades elegibles", "content": "Texto."},
            {"step_id": 2, "title": "S.2. Componentes con potencial de financiamiento verde", "content": "",
             "table": {"rows": [
                 {"componente": "Planta potabilizadora[[c:1]]", "objetivo": "Adaptación",
                  "categoria": "Agua", "monto_usd": 12000000, "fundamento": "Reduce el riesgo hídrico."},
                 {"componente": "", "objetivo": "Mitigación"},
             ]}},
        ])
        components = ecofilia_green_components(run)
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["componente"], "Planta potabilizadora")
        self.assertEqual(components[0]["monto_usd"], 12000000)

    def test_an_iet_without_s2_suggests_nothing(self):
        self.assertEqual(ecofilia_green_components(_Run([_s1([])])), [])
        self.assertEqual(ecofilia_green_components(None), [])

    def test_the_criterio_compares_the_greening_with_s2(self):
        notes = {"enverdecimiento": {
            "componentes": [
                {"componente": "Planta potabilizadora", "objetivo": "Adaptación", "monto_usd": 12000000,
                 "origen": "insumo", "estado": "incluido"},
            ],
            "oportunidades": [],
        }}
        sugeridos = [{"componente": "Planta potabilizadora", "objetivo": "Adaptación", "monto_usd": 12000000}]
        criterio = build_criterio_ejecutivo(notes, componentes_ecofilia=sugeridos)
        self.assertIn("(→ coincide con el insumo)", criterio)
        self.assertIn("Lo que sugería el insumo de Ecofilia: Planta potabilizadora (Adaptación, USD 12,000,000).", criterio)
        self.assertNotIn("Oportunidades que sugería", criterio)
