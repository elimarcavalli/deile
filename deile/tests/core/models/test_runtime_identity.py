"""Regressão: o ``system_instruction`` enviado ao LLM precisa carregar a
identidade runtime real (provider:model) para evitar que o modelo invente
ser "Claude" ou outro modelo qualquer quando perguntado sobre sua identidade.

Cenário reportado pelo usuário (commit anterior): após startup sem ``/model``,
o footer mostrava ``deepseek:deepseek-v4-flash`` corretamente — provando que
o DeepSeek estava de fato processando — mas a resposta textual afirmava ser
Claude/Anthropic. Causa raiz: ``system_instruction`` não dizia qual modelo
estava rodando, e a forte prior do treino de assistant agents leva o modelo
a chutar "Claude".

Estes testes validam o helper ``ModelProvider._compose_system_instruction``:
ele acrescenta um bloco de identidade runtime que:
  * Identifica ``DEILE`` como o agente
  * Cita ``provider_id:model_name`` real
  * Proíbe explicitamente a invenção de outras identidades
"""
from __future__ import annotations

import pytest

from deile.core.models.base import ModelProvider, ModelSize, ModelType


class _DummyProvider(ModelProvider):
    """Concreto mínimo de ``ModelProvider`` para testar o helper sem rede."""

    def __init__(self, provider_id: str, model_name: str):
        super().__init__(model_name=model_name)
        self._pid = provider_id

    @property
    def provider_name(self) -> str:
        return self._pid

    @property
    def provider_id(self) -> str:
        return self._pid

    @property
    def supported_types(self):
        return [ModelType.TEXT]

    @property
    def model_size(self) -> ModelSize:
        return ModelSize.MEDIUM

    async def generate(self, messages, system_instruction=None, **kwargs):
        raise NotImplementedError

    async def generate_stream(self, messages, system_instruction=None, tools=None, **kwargs):
        if False:
            yield  # pragma: no cover — async generator marker


@pytest.mark.unit
def test_compose_with_existing_persona_appends_runtime_block():
    """Persona base é preservada; runtime block vai NO FINAL (cache-friendly)."""
    provider = _DummyProvider("deepseek", "deepseek-v4-flash")
    persona = "Você é DEILE, agente sênior."
    composed = provider._compose_system_instruction(persona)
    assert composed.startswith("Você é DEILE, agente sênior.")
    assert "<runtime_identity>" in composed
    assert composed.index(persona) < composed.index("<runtime_identity>")


@pytest.mark.unit
def test_compose_block_names_provider_and_model_verbatim():
    """O bloco runtime cita ``provider:model`` exato."""
    provider = _DummyProvider("deepseek", "deepseek-v4-flash")
    composed = provider._compose_system_instruction(None)
    assert "deepseek:deepseek-v4-flash" in composed


@pytest.mark.unit
def test_compose_block_forbids_claiming_other_identities():
    """O bloco proíbe explicitamente o modelo de inventar identidade."""
    provider = _DummyProvider("deepseek", "deepseek-v4-flash")
    composed = provider._compose_system_instruction(None)
    # Anti-impersonação: nomes dos provedores comuns devem aparecer no negativo.
    assert "Claude" in composed
    assert "GPT" in composed
    assert "Gemini" in composed
    assert "NUNCA" in composed.upper() or "NEVER" in composed.upper()


@pytest.mark.unit
def test_compose_with_no_existing_instruction_returns_only_runtime_block():
    """Sem persona base, o composto é apenas o bloco runtime."""
    provider = _DummyProvider("anthropic", "claude-haiku-4-5")
    composed = provider._compose_system_instruction(None)
    assert composed.startswith("<runtime_identity>")
    assert "anthropic:claude-haiku-4-5" in composed


@pytest.mark.unit
def test_compose_different_providers_produce_different_blocks():
    """Cache awareness: blocos diferem quando provider/model diferem."""
    a = _DummyProvider("openai", "gpt-5.3")
    b = _DummyProvider("gemini", "gemini-2.5-pro")
    persona = "shared persona"
    assert a._compose_system_instruction(persona) != b._compose_system_instruction(persona)


@pytest.mark.unit
def test_compose_preserves_persona_prefix_for_cache():
    """Persona vai antes do bloco runtime → prefixo cacheável estável."""
    persona = "Você é DEILE." + (" " * 1000) + "Fim do persona."
    a = _DummyProvider("openai", "gpt-5.3")
    b = _DummyProvider("openai", "gpt-5.3-mini")
    ca = a._compose_system_instruction(persona)
    cb = b._compose_system_instruction(persona)
    # Prefixos coincidem até a quebra para o bloco runtime — o que permite
    # ao cache anthropic/openai reusar a entrada do persona.
    common_prefix = "".join(c1 for c1, c2 in zip(ca, cb) if c1 == c2)
    assert persona in common_prefix
