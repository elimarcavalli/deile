"""OpenRouter provider — OpenAI-compatible gateway, thin subclass of OpenAIProvider.

OpenRouter (https://openrouter.ai) exposes a single OpenAI-compatible endpoint
(``/api/v1``) that fans out to many upstream vendors (Anthropic, OpenAI, Google,
DeepSeek, Qwen, …) under one API key (``OPENROUTER_API_KEY``). The wire protocol
is identical to OpenAI's chat-completions API, so the only deltas from
:class:`OpenAIProvider` are:

1. **Identity** — ``provider_id``/``provider_name`` = ``"openrouter"``.
2. **Authoritative cost** — OpenRouter applies markup and routes dynamically, so
   the static catalog price never matches the billed amount. When the request
   asks for ``usage: {include: true}``, the response carries ``usage.cost`` (in
   USD credits). We surface that as the authoritative ``cost_estimate`` and fall
   back to the catalog table only when the field is absent.
3. **Cached tokens** — OpenRouter reports cache reads under the OpenAI shape
   (``prompt_tokens_details.cached_tokens``), inherited verbatim.

PRIVACIDADE: o prompt trafega por um hop adicional (OpenRouter) antes de chegar
ao provedor terceiro. Aceitável para código do projeto; documentado em
``DECISOES.md`` #50 e em ``model_providers.yaml``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from deile.core.models.base import ModelType, ModelUsage
from deile.core.models.catalog import ModelHandle
from deile.core.models.openai_provider import OpenAIProvider
from deile.core.models.provider_config import ProviderConfig


class OpenRouterProvider(OpenAIProvider):
    """ModelProvider for OpenRouter (OpenAI-compatible multi-vendor gateway)."""

    def __init__(
        self,
        model_handle: ModelHandle,
        provider_config: ProviderConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_handle, provider_config, **kwargs)

    @property
    def provider_name(self) -> str:
        return "openrouter"

    @property
    def provider_id(self) -> str:
        return "openrouter"

    @property
    def supported_types(self) -> List[ModelType]:
        # OpenRouter routes to vision-capable upstreams too, but the catalog gates
        # actual capabilities per model; CHAT/CODE/VISION is the safe superset.
        return [ModelType.CHAT, ModelType.CODE, ModelType.VISION]

    def _provider_extra_body(self) -> Dict[str, Any]:
        """Ask OpenRouter to include the billed cost + token accounting in the
        response (``usage.cost`` / ``usage.completion_tokens`` etc.)."""
        return {"usage": {"include": True}}

    @staticmethod
    def _reported_cost_from(obj: Any) -> Optional[float]:
        """Extract OpenRouter's billed cost (USD) from a response or usage object.

        Accepts either the full chat-completion ``response`` (``response.usage.cost``)
        or a bare ``usage`` object (``usage.cost``) — the streaming path passes the
        latter. Returns ``None`` when the field is absent or non-numeric, so the
        caller falls back to the catalog price.
        """
        usage = getattr(obj, "usage", obj)
        cost = getattr(usage, "cost", None)
        if cost is None and isinstance(usage, dict):
            cost = usage.get("cost")
        if cost is None:
            return None
        try:
            return float(cost)
        except (TypeError, ValueError):
            return None

    def _stamp_reported_cost(self, usage: ModelUsage, response: Any) -> None:
        reported = self._reported_cost_from(response)
        if reported is not None:
            usage.extra["reported_cost_usd"] = reported

    def estimate_cost(self, usage: ModelUsage) -> float:
        """Prefer OpenRouter's reported (billed) cost; fall back to the catalog.

        ``reported_cost_usd`` is stamped by :meth:`_stamp_reported_cost` from the
        response's ``usage.cost``. When present it is authoritative (it already
        bakes in OpenRouter's markup and the dynamically-routed upstream price),
        so the static catalog table is bypassed entirely — eliminating the
        systematic undercount that a fixed price table would otherwise cause.
        """
        reported = usage.extra.get("reported_cost_usd")
        if reported is not None:
            try:
                return round(float(reported), 8)
            except (TypeError, ValueError):
                pass
        return super().estimate_cost(usage)
