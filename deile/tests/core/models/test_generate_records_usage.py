"""Tests do Bug 2 — `generate()` simples persiste usage em todos os providers.

Antes deste fix, só `chat_with_tools()` chamava `_record_usage`. O método
`generate()` (usado por `agent.py:2205/2458/2515` — summarização, intent
classification, etc) construía o ModelUsage corretamente mas nunca
persistia — burn invisível no painel de custos.

Estes tests lockam o contrato em AnthropicProvider e OpenAIProvider (que
DeepSeekProvider herda) sem fazer chamada de rede. Gemini já tem cobertura
equivalente em test_gemini_provider_usage.py.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from deile.core.models.anthropic_provider import AnthropicProvider
from deile.core.models.openai_provider import OpenAIProvider


@pytest.mark.asyncio
class TestAnthropicGenerateRecords:
    def _fake_response(self, prompt=120, completion=60, cached=10):
        return SimpleNamespace(
            content=[SimpleNamespace(text="oi")],
            usage=SimpleNamespace(
                input_tokens=prompt,
                output_tokens=completion,
                cache_read_input_tokens=cached,
            ),
            stop_reason="end_turn",
        )

    def _make_provider(self) -> AnthropicProvider:
        prov = MagicMock(spec=AnthropicProvider)
        prov.provider_id = "anthropic"
        prov.model_name = "claude-opus-4-7"
        prov._compose_system_instruction = MagicMock(return_value="sys")
        prov._extract_system = MagicMock(return_value="sys")
        prov._to_anthropic_messages = MagicMock(return_value=[])
        prov._system_blocks = MagicMock(return_value=[])
        prov.estimate_cost = MagicMock(return_value=0.0015)
        prov._update_stats = MagicMock()
        prov._record_usage = AsyncMock()
        prov._record_failed_usage = AsyncMock()
        prov._client = MagicMock()
        prov._client.messages = MagicMock()
        prov._client.messages.create = AsyncMock(
            return_value=self._fake_response(),
        )
        return prov

    async def test_success_path_calls_record_usage(self):
        prov = self._make_provider()
        with patch("deile.core.models.anthropic_provider.DEFAULT_MAX_OUTPUT_TOKENS",
                   4096):
            result = await AnthropicProvider.generate(
                prov, messages=[], session_id="anth-1",
            )

        assert result.usage.prompt_tokens == 120
        assert result.usage.cached_tokens == 10
        prov._record_usage.assert_awaited_once()
        kwargs = prov._record_usage.call_args.kwargs
        assert kwargs["session_id"] == "anth-1"
        assert kwargs["success"] is True
        prov._record_failed_usage.assert_not_awaited()

    async def test_session_id_defaults_to_default(self):
        prov = self._make_provider()
        with patch("deile.core.models.anthropic_provider.DEFAULT_MAX_OUTPUT_TOKENS",
                   4096):
            await AnthropicProvider.generate(prov, messages=[])
        kwargs = prov._record_usage.call_args.kwargs
        assert kwargs["session_id"] == "default"

    async def test_api_error_calls_record_failed_usage(self):
        import anthropic
        from deile.core.models.errors import ProviderInvocationError
        prov = self._make_provider()
        prov._client.messages.create = AsyncMock(
            side_effect=anthropic.APIError(
                "rate limit", request=MagicMock(), body=None,
            ),
        )
        with patch("deile.core.models.anthropic_provider.DEFAULT_MAX_OUTPUT_TOKENS",
                   4096):
            with pytest.raises(ProviderInvocationError):
                await AnthropicProvider.generate(
                    prov, messages=[], session_id="anth-err",
                )

        prov._record_failed_usage.assert_awaited_once()
        kwargs = prov._record_failed_usage.call_args.kwargs
        assert kwargs["session_id"] == "anth-err"
        prov._record_usage.assert_not_awaited()


@pytest.mark.asyncio
class TestOpenAIGenerateRecords:
    def _fake_response(self, prompt=80, completion=40):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="oi"),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(
                prompt_tokens=prompt, completion_tokens=completion,
            ),
        )

    def _make_provider(self) -> OpenAIProvider:
        prov = MagicMock(spec=OpenAIProvider)
        prov.provider_id = "openai"
        prov.model_name = "gpt-5.4"
        prov._compose_system_instruction = MagicMock(return_value="sys")
        prov._to_openai_messages = MagicMock(return_value=[])
        prov._extract_cached_tokens = MagicMock(return_value=5)
        prov.estimate_cost = MagicMock(return_value=0.0008)
        prov._update_stats = MagicMock()
        prov._record_usage = AsyncMock()
        prov._record_failed_usage = AsyncMock()
        prov._client = MagicMock()
        prov._client.chat = MagicMock()
        prov._client.chat.completions = MagicMock()
        prov._client.chat.completions.create = AsyncMock(
            return_value=self._fake_response(),
        )
        return prov

    async def test_success_path_calls_record_usage(self):
        prov = self._make_provider()
        with patch("deile.core.models.openai_provider.DEFAULT_MAX_OUTPUT_TOKENS",
                   4096):
            result = await OpenAIProvider.generate(
                prov, messages=[], session_id="oai-1",
            )

        assert result.usage.prompt_tokens == 80
        assert result.usage.cached_tokens == 5
        prov._record_usage.assert_awaited_once()
        kwargs = prov._record_usage.call_args.kwargs
        assert kwargs["session_id"] == "oai-1"
        assert kwargs["success"] is True

    async def test_session_id_popped_from_kwargs(self):
        """`session_id` é metadado interno do DEILE — não deve chegar no
        SDK do OpenAI (causaria `TypeError: unexpected keyword argument`)."""
        prov = self._make_provider()
        with patch("deile.core.models.openai_provider.DEFAULT_MAX_OUTPUT_TOKENS",
                   4096):
            await OpenAIProvider.generate(
                prov, messages=[], session_id="oai-pop",
            )
        # SDK foi chamado SEM session_id nos kwargs.
        call = prov._client.chat.completions.create.call_args
        assert "session_id" not in call.kwargs

    async def test_api_error_calls_record_failed_usage(self):
        import openai
        from deile.core.models.errors import ProviderInvocationError
        prov = self._make_provider()
        prov._client.chat.completions.create = AsyncMock(
            side_effect=openai.APIError(
                "rate limit", request=MagicMock(), body=None,
            ),
        )
        with patch("deile.core.models.openai_provider.DEFAULT_MAX_OUTPUT_TOKENS",
                   4096):
            with pytest.raises(ProviderInvocationError):
                await OpenAIProvider.generate(
                    prov, messages=[], session_id="oai-err",
                )
        prov._record_failed_usage.assert_awaited_once()
        kwargs = prov._record_failed_usage.call_args.kwargs
        assert kwargs["session_id"] == "oai-err"
