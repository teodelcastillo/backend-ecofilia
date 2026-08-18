"""
Parámetros tipados: defaults, tipado y vocabulario cerrado, antes de correr.

Sin esto, un ``{{marco}}`` sin valor no fallaba: viajaba literal dentro del
prompt y el modelo lo leía como texto. Se valida al lanzar porque ahí hay
alguien esperando la respuesta — el lugar más barato para corregirlo.
"""
from dataclasses import dataclass, field

from django.test import SimpleTestCase

from apps.skill import parameters as paramsmod


@dataclass
class FakeParameter:
    key: str
    label: str = ""
    param_type: str = "text"
    default_value: str = ""
    required: bool = False
    options: list = field(default_factory=list)


class DefaultsTests(SimpleTestCase):
    def test_missing_value_falls_back_to_the_declared_default(self):
        parameters = [FakeParameter(key="marco", default_value="GRI")]
        resolved, issues = paramsmod.resolve_input_values(parameters, {})

        self.assertEqual(resolved["marco"], "GRI")
        self.assertEqual(issues, [])

    def test_missing_required_without_default_is_blocking(self):
        parameters = [FakeParameter(key="anio", required=True)]
        resolved, issues = paramsmod.resolve_input_values(parameters, {})

        self.assertNotIn("anio", resolved)
        self.assertEqual(issues[0]["problem"], paramsmod.MISSING_REQUIRED)
        self.assertEqual(paramsmod.blocking_issues(issues), issues)

    def test_missing_optional_without_default_is_simply_absent(self):
        parameters = [FakeParameter(key="nota", required=False)]
        resolved, issues = paramsmod.resolve_input_values(parameters, {})

        self.assertNotIn("nota", resolved)
        self.assertEqual(issues, [])


class TypingTests(SimpleTestCase):
    def test_number_is_coerced(self):
        parameters = [FakeParameter(key="anio", param_type="number")]
        resolved, issues = paramsmod.resolve_input_values(parameters, {"anio": "2024"})

        self.assertEqual(resolved["anio"], 2024)
        self.assertEqual(issues, [])

    def test_unparseable_number_is_reported(self):
        parameters = [FakeParameter(key="anio", param_type="number")]
        _, issues = paramsmod.resolve_input_values(parameters, {"anio": "dos mil"})

        self.assertEqual(issues[0]["problem"], paramsmod.INVALID_NUMBER)

    def test_boolean_accepts_common_spellings(self):
        parameters = [FakeParameter(key="incluir_anexos", param_type="boolean")]
        resolved, _ = paramsmod.resolve_input_values(parameters, {"incluir_anexos": "sí"})
        self.assertIs(resolved["incluir_anexos"], True)

    def test_date_must_be_iso(self):
        parameters = [FakeParameter(key="fecha_corte", param_type="date")]
        _, issues = paramsmod.resolve_input_values(parameters, {"fecha_corte": "18/08/2026"})
        self.assertEqual(issues[0]["problem"], paramsmod.INVALID_DATE)

        resolved, issues_ok = paramsmod.resolve_input_values(
            parameters, {"fecha_corte": "2026-08-18"}
        )
        self.assertEqual(resolved["fecha_corte"], "2026-08-18")
        self.assertEqual(issues_ok, [])


class EnumTests(SimpleTestCase):
    def test_value_outside_the_vocabulary_is_reported(self):
        parameters = [FakeParameter(key="marco", param_type="enum", options=["GRI", "ISSB"])]
        _, issues = paramsmod.resolve_input_values(parameters, {"marco": "TCFD"})
        self.assertEqual(issues[0]["problem"], paramsmod.INVALID_ENUM)

    def test_case_difference_is_normalized_not_a_violation(self):
        parameters = [FakeParameter(key="marco", param_type="enum", options=["GRI", "ISSB"])]
        resolved, issues = paramsmod.resolve_input_values(parameters, {"marco": "gri"})
        self.assertEqual(resolved["marco"], "GRI")
        self.assertEqual(issues, [])

    def test_default_is_validated_against_its_own_type(self):
        """Un default que no cumple su propio tipo es un error de definición,
        no de quien llama."""
        parameters = [
            FakeParameter(key="marco", param_type="enum", options=["GRI"], default_value="TCFD")
        ]
        _, issues = paramsmod.resolve_input_values(parameters, {})
        self.assertEqual(issues[0]["problem"], paramsmod.INVALID_DEFAULT)


class UnknownKeyTests(SimpleTestCase):
    def test_unknown_key_is_kept_but_reported_non_blocking(self):
        resolved, issues = paramsmod.resolve_input_values([], {"typo_de_marco": "GRI"})

        self.assertEqual(resolved["typo_de_marco"], "GRI")
        self.assertEqual(issues[0]["problem"], paramsmod.UNKNOWN_PARAMETER)
        self.assertEqual(paramsmod.blocking_issues(issues), [])


class DescribeIssueTests(SimpleTestCase):
    def test_every_problem_kind_has_a_human_message(self):
        for problem in (
            paramsmod.MISSING_REQUIRED,
            paramsmod.INVALID_NUMBER,
            paramsmod.INVALID_DATE,
            paramsmod.INVALID_ENUM,
            paramsmod.INVALID_DEFAULT,
            paramsmod.UNKNOWN_PARAMETER,
        ):
            message = paramsmod.describe_issue({
                "key": "marco", "label": "Marco", "problem": problem,
                "received": "x", "allowed_values": ["GRI"],
            })
            self.assertIsInstance(message, str)
            self.assertGreater(len(message), 0)
