"""
Resolución del modelo según el tipo de skill.

Un workflow encadena pasos que razonan sobre documentos largos apoyándose en
las salidas anteriores — el tier DEEP. Una skill de un solo paso no lo
necesita y se queda en el balanceado.
"""
from unittest import mock

from django.test import SimpleTestCase

from apps.document.utils.llm import (
    ROLE_BALANCED,
    ROLE_DEEP,
    effective_chat_model,
    resolve_model,
)


def anthropic_env(**extra):
    """
    Entorno con Anthropic activo y sin overrides por tier.

    `clear=True` es deliberado: un `LLM_MODEL_DEEP` presente en la máquina que
    corre los tests pisaría el default y el test pasaría por el motivo
    equivocado.
    """
    return mock.patch.dict(
        "os.environ", {"LLM_PROVIDER": "anthropic", **extra}, clear=True
    )


class AnthropicTierDefaultsTests(SimpleTestCase):
    """Los defaults por tier no deben quedarse en una generación vieja."""

    def test_tiers_resolve_to_current_models(self):
        with anthropic_env():
            self.assertEqual(resolve_model(ROLE_DEEP), "claude-opus-5")
            self.assertEqual(resolve_model(ROLE_BALANCED), "claude-sonnet-5")

    def test_explicit_env_still_wins_over_the_default(self):
        with anthropic_env(LLM_MODEL_DEEP="claude-fable-5"):
            self.assertEqual(resolve_model(ROLE_DEEP), "claude-fable-5")


class EffectiveChatModelRoleTests(SimpleTestCase):
    def test_legacy_openai_id_is_upgraded_to_the_requested_tier(self):
        """Las skills guardan `gpt-4o-mini`; el tier lo resuelve el provider."""
        with mock.patch.dict("os.environ", {"LLM_PROVIDER": "anthropic"}, clear=False):
            self.assertEqual(
                effective_chat_model("gpt-4o-mini", ROLE_DEEP), "claude-opus-5"
            )

    def test_role_defaults_to_balanced(self):
        with mock.patch.dict("os.environ", {"LLM_PROVIDER": "anthropic"}, clear=False):
            self.assertEqual(effective_chat_model("gpt-4o-mini"), "claude-sonnet-5")

    def test_an_explicit_claude_id_is_honored_over_the_tier(self):
        with mock.patch.dict("os.environ", {"LLM_PROVIDER": "anthropic"}, clear=False):
            self.assertEqual(
                effective_chat_model("claude-haiku-4-5", ROLE_DEEP), "claude-haiku-4-5"
            )

    def test_openai_provider_preserves_the_stored_id(self):
        """Sin el switch de provider, nada cambia."""
        with mock.patch.dict("os.environ", {"LLM_PROVIDER": "openai"}, clear=False):
            self.assertEqual(
                effective_chat_model("gpt-4o-mini", ROLE_DEEP), "gpt-4o-mini"
            )
