"""
Una respuesta sin texto tiene que decir por qué.

El caso real: el paso V.3 de enverdecimiento volvió «200 OK» a los tres minutos
y sin texto, y el error sólo decía «empty response». No había forma de saber
si el modelo agotó su salida, si el contexto llenó la ventana o si se negó.
"""
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.document.utils import llm


def _response(stop_reason, blocks, *, input_tokens=900_000, output_tokens=16_000):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type=t) for t in blocks],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class EmptyResponseTestCase(SimpleTestCase):
    def _call(self, response):
        client = mock.Mock()
        client.messages.create.return_value = response
        with mock.patch.object(llm, "_anthropic_client", return_value=client):
            return llm.anthropic_chat_completion(
                [{"role": "user", "content": "hola"}], model="claude-opus-5"
            )

    def test_output_limit_spent_thinking_says_so(self):
        with self.assertRaisesRegex(ValueError, "agotó su límite de salida.*razonando"):
            self._call(_response("max_tokens", ["thinking"]))

    def test_full_context_window_says_so(self):
        with self.assertRaisesRegex(ValueError, "llenó su ventana"):
            self._call(_response("model_context_window_exceeded", []))

    def test_refusal_says_so(self):
        with self.assertRaisesRegex(ValueError, "se negó"):
            self._call(_response("refusal", []))

    def test_unknown_reason_is_named(self):
        with self.assertRaisesRegex(ValueError, "motivo: end_turn"):
            self._call(_response("end_turn", []))

    def test_diagnosis_is_logged(self):
        with self.assertLogs(llm.logger, level="WARNING") as logs:
            with self.assertRaises(ValueError):
                self._call(_response("max_tokens", ["thinking"], input_tokens=812_345))
        self.assertIn("stop_reason=max_tokens", logs.output[0])
        self.assertIn("input_tokens=812345", logs.output[0])
