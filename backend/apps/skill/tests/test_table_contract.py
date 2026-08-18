"""
El contrato de salida tabular: qué pasa cuando el modelo no lo cumple.

El motor aceptaba esquemas con columnas tipadas y vocabularios cerrados desde
hace tiempo, pero no los hacía cumplir. Un valor fuera del enum se convertía en
celda vacía, ``required`` se recibía y no se usaba, y un JSON inválido degradaba
el paso a texto libre — así que la determinación que debía ser un contrato
terminaba siendo prosa, y la corrida terminaba "bien".

Eso es lo que rompe la reproducibilidad por el lado que más importa: al comparar
dos corridas, una violación del contrato se veía igual que un dato que no
estaba.

La rigidez es por paso porque este no va a ser el único workflow: una
determinación auditable y una exploración no piden lo mismo.
"""
from django.test import SimpleTestCase

from apps.skill.services import (
    CELL_INVALID_ENUM,
    CELL_INVALID_NUMBER,
    CELL_MISSING_REQUIRED,
    TableContractError,
    coerce_table_output,
    validate_table_cell_value,
)

DETERMINACION = {
    "name": "Determinación",
    "columns": [
        {
            "key": "criterio",
            "label": "Criterio",
            "type": "text",
            "required": True,
            "allowed_values": [],
        },
        {
            "key": "resultado",
            "label": "Resultado",
            "type": "enum",
            "required": True,
            "allowed_values": ["Alineado", "No alineado", "Sin información"],
        },
    ],
}


def salida(rows) -> str:
    import json

    return json.dumps({"rows": rows})


class CellValidationTests(SimpleTestCase):
    def test_required_empty_cell_is_reported(self):
        """``required`` se recibía y no se usaba: podías marcar una columna como
        obligatoria y el motor la dejaba vacía sin decir nada."""
        value, problem = validate_table_cell_value(
            value=None, col_type="text", required=True, allowed_values=set()
        )

        self.assertEqual(value, "")
        self.assertEqual(problem, CELL_MISSING_REQUIRED)

    def test_optional_empty_cell_is_not_a_problem(self):
        _, problem = validate_table_cell_value(
            value="", col_type="text", required=False, allowed_values=set()
        )

        self.assertIsNone(problem)

    def test_value_outside_the_vocabulary_is_reported(self):
        """Antes se volvía celda vacía, indistinguible de "no contestó"."""
        value, problem = validate_table_cell_value(
            value="Parcialmente alineado",
            col_type="enum",
            required=True,
            allowed_values={"Alineado", "No alineado"},
        )

        self.assertEqual(value, "")
        self.assertEqual(problem, CELL_INVALID_ENUM)

    def test_case_differences_are_format_not_a_violation(self):
        value, problem = validate_table_cell_value(
            value="ALINEADO",
            col_type="enum",
            required=True,
            allowed_values={"Alineado", "No alineado"},
        )

        self.assertEqual(value, "Alineado")
        self.assertIsNone(problem)

    def test_unparseable_number_is_reported(self):
        value, problem = validate_table_cell_value(
            value="varios", col_type="number", required=False, allowed_values=set()
        )

        self.assertEqual(value, "")
        self.assertEqual(problem, CELL_INVALID_NUMBER)


class LenientPolicyTests(SimpleTestCase):
    """Tolerante no significa ciego: normaliza lo que puede y deja registro."""

    def test_invalid_values_are_recorded_not_swallowed(self):
        table = coerce_table_output(
            output_text=salida([{"criterio": "Adaptación", "resultado": "Quizás"}]),
            table_schema=DETERMINACION,
            strict=False,
        )

        self.assertEqual(table["rows"][0]["resultado"], "")
        self.assertEqual(len(table["issues"]), 1)
        self.assertEqual(table["issues"][0]["column"], "resultado")
        self.assertEqual(table["issues"][0]["received"], "Quizás")

    def test_a_clean_table_reports_no_issues(self):
        table = coerce_table_output(
            output_text=salida([{"criterio": "Adaptación", "resultado": "Alineado"}]),
            table_schema=DETERMINACION,
            strict=False,
        )

        self.assertEqual(table["issues"], [])
        self.assertEqual(table["rows"][0]["resultado"], "Alineado")


class StrictPolicyTests(SimpleTestCase):
    def test_value_outside_the_vocabulary_fails(self):
        with self.assertRaises(TableContractError) as ctx:
            coerce_table_output(
                output_text=salida([{"criterio": "Adaptación", "resultado": "Quizás"}]),
                table_schema=DETERMINACION,
                strict=True,
            )

        self.assertIn("Quizás", str(ctx.exception))
        self.assertIn("Alineado", str(ctx.exception), "el mensaje dice qué se esperaba")

    def test_missing_required_cell_fails(self):
        with self.assertRaises(TableContractError):
            coerce_table_output(
                output_text=salida([{"resultado": "Alineado"}]),
                table_schema=DETERMINACION,
                strict=True,
            )

    def test_row_that_is_not_an_object_fails(self):
        """En tolerante se saltea en silencio, que es cómo una fila entera
        desaparecía de una determinación sin dejar rastro."""
        with self.assertRaises(TableContractError):
            coerce_table_output(
                output_text=salida(["Alineado"]),
                table_schema=DETERMINACION,
                strict=True,
            )

        tolerante = coerce_table_output(
            output_text=salida(["Alineado"]), table_schema=DETERMINACION, strict=False
        )
        self.assertEqual(tolerante["rows"], [])
        self.assertEqual(tolerante["issues"][0]["problem"], "row_not_object")

    def test_a_compliant_table_passes_either_way(self):
        filas = [{"criterio": "Adaptación", "resultado": "Sin información"}]

        for strict in (True, False):
            with self.subTest(strict=strict):
                table = coerce_table_output(
                    output_text=salida(filas),
                    table_schema=DETERMINACION,
                    strict=strict,
                )
                self.assertEqual(table["rows"], filas)

    def test_unparseable_json_is_a_contract_violation_too(self):
        """Un JSON que no parsea tiene que subir como violación del contrato.

        Levantaba un `ValueError` plano, y el runner discriminaba por tipo de
        excepción: la rama de degradación lo atrapaba y convertía el paso en
        texto **aun en modo estricto** — exactamente lo que la política venía a
        eliminar. `TableContractError` hereda de `ValueError`, así que el modo
        tolerante lo sigue atrapando y degradando como antes.
        """
        for salida_rota in ("Esto no es JSON", '{"sin": "rows"}', "[1, 2, 3]"):
            with self.subTest(salida=salida_rota):
                with self.assertRaises(TableContractError):
                    coerce_table_output(
                        output_text=salida_rota,
                        table_schema=DETERMINACION,
                        strict=True,
                    )

    def test_error_message_names_the_row(self):
        """Un esquema con veinte filas necesita decir cuál falló."""
        with self.assertRaises(TableContractError) as ctx:
            coerce_table_output(
                output_text=salida(
                    [
                        {"criterio": "A", "resultado": "Alineado"},
                        {"criterio": "B", "resultado": "Inventado"},
                    ]
                ),
                table_schema=DETERMINACION,
                strict=True,
            )

        self.assertIn("Fila 1", str(ctx.exception))
