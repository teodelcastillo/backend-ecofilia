"""
Controles por paso que el autor del workflow maneja desde el editor.

Sin base: los pasos y las secciones son objetos de prueba, y el cliente de
Anthropic se reemplaza por un doble que registra el pedido.
"""
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from apps.document.utils import llm
from apps.skill import prompt_text
from apps.skill.services import select_history


@dataclass
class FakeStep:
    position: int
    history_mode: str = "auto"
    history_positions: list = field(default_factory=list)


def _steps(n: int) -> list[FakeStep]:
    return [FakeStep(position=i) for i in range(1, n + 1)]


def _sections(n: int) -> list[tuple[str, str]]:
    return [(f"Paso {i}", f"Cuerpo del paso {i}. " * 200) for i in range(1, n + 1)]


class SelectHistoryTests(SimpleTestCase):
    def test_default_is_unchanged(self):
        """Sin elegir nada, el paso ve lo mismo que antes: todo el historial,
        con las últimas secciones completas."""
        steps, sections = _steps(5), _sections(4)
        current = FakeStep(position=5)

        rendered = select_history(current, steps, sections)

        self.assertEqual(len(rendered), 4)
        self.assertIn("### Paso 1", rendered[0])

    def test_selected_sections_only_and_complete(self):
        steps, sections = _steps(5), _sections(4)
        current = FakeStep(position=5, history_mode="selected", history_positions=[1, 3])

        rendered = select_history(current, steps, sections)

        self.assertEqual([r.split("\n")[0] for r in rendered], ["### Paso 1", "### Paso 3"])
        # Completas: si el autor las nombró, las necesita enteras.
        self.assertTrue(all("[…]" not in r for r in rendered))
        self.assertEqual(rendered[0], f"### Paso 1\n{sections[0][1]}")

    def test_selected_with_nothing_chosen_sees_nothing(self):
        steps, sections = _steps(3), _sections(2)
        current = FakeStep(position=3, history_mode="selected", history_positions=[])

        self.assertEqual(select_history(current, steps, sections), [])


class ReasoningEffortRequestTests(SimpleTestCase):
    def _request(self, model: str, effort):
        return llm._build_request(
            [{"role": "user", "content": "hola"}],
            model=model, temperature=None, max_tokens=100, effort=effort,
        )

    def test_effort_travels_on_models_that_accept_it(self):
        self.assertEqual(
            self._request("claude-sonnet-5", "low")["output_config"], {"effort": "low"}
        )

    def test_no_effort_means_the_model_default(self):
        self.assertNotIn("output_config", self._request("claude-sonnet-5", None))
        self.assertNotIn("output_config", self._request("claude-sonnet-5", ""))

    def test_haiku_never_gets_effort(self):
        """Haiku 4.5 responde 400 si se lo mandan."""
        self.assertNotIn("output_config", self._request("claude-haiku-4-5", "max"))

    def test_effort_reaches_the_api_call(self):
        response = SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="ok", citations=None)],
            usage=None,
        )
        client = mock.Mock()
        client.messages.create.return_value = response
        with mock.patch.object(llm, "_anthropic_client", return_value=client):
            llm.anthropic_chat_completion(
                [{"role": "user", "content": "hola"}], model="claude-opus-5", effort="xhigh",
            )
        self.assertEqual(
            client.messages.create.call_args.kwargs["output_config"], {"effort": "xhigh"}
        )


class PromptLanguageTests(SimpleTestCase):
    def test_labels_follow_the_platform_language(self):
        self.assertEqual(prompt_text.text("task", title="B.1"), "## Tarea: B.1")

    def test_unknown_language_falls_back_instead_of_mixing(self):
        with mock.patch.dict("os.environ", {"PLATFORM_LANGUAGE": "xx"}):
            self.assertEqual(prompt_text.text("task", title="B.1"), "## Tarea: B.1")
