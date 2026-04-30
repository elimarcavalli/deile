"""DeepSeek provider — OpenAI-compatible API, thin subclass of OpenAIProvider."""

from __future__ import annotations

from typing import Any, List

from deile.core.models.catalog import ModelHandle
from deile.core.models.openai_provider import OpenAIProvider
from deile.core.models.provider_config import ProviderConfig
from deile.core.models.base import ModelType


class DeepSeekProvider(OpenAIProvider):
    """ModelProvider for DeepSeek models (OpenAI-compatible REST API)."""

    def __init__(
        self,
        model_handle: ModelHandle,
        provider_config: ProviderConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_handle, provider_config, **kwargs)

    @property
    def provider_name(self) -> str:
        return "deepseek"

    @property
    def provider_id(self) -> str:
        return "deepseek"

    @property
    def supported_types(self) -> List[ModelType]:
        return [ModelType.CHAT, ModelType.CODE]

    @staticmethod
    def _extract_cached_tokens(response: Any) -> int:
        """DeepSeek exposes prompt_cache_hit_tokens instead of prompt_tokens_details."""
        try:
            return getattr(response.usage, "prompt_cache_hit_tokens", None) or 0
        except AttributeError:
            return 0
