"""Model catalog: loads and queries ModelHandle entries from model_providers.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional

import yaml

from deile.core.models.tier import ModelTier


@dataclass(frozen=True)
class ModelPricing:
    """Per-token pricing for a model (all values in USD per 1M tokens)."""

    input_per_1m_usd: float
    output_per_1m_usd: float
    cached_input_per_1m_usd: Optional[float] = None


@dataclass(frozen=True)
class ModelHandle:
    """Immutable descriptor for one model in the catalog."""

    provider_id: str           # "anthropic" | "openai" | "deepseek" | "gemini"
    model_id: str              # e.g. "claude-opus-4-7"
    tier: ModelTier
    pricing: ModelPricing
    context_window: int
    capabilities: FrozenSet[str]   # {"function_calling", "vision", "streaming", "caching"}
    display_name: str
    label: str                 # "flagship" | "balanced" | "fast" | ...


class ModelCatalog:
    """In-memory catalog of all configured models, loaded from YAML."""

    def __init__(self, handles: List[ModelHandle]) -> None:
        self._handles = handles
        self._index: Dict[str, ModelHandle] = {
            f"{h.provider_id}:{h.model_id}": h for h in handles
        }

    @classmethod
    def from_yaml(cls, path: Path) -> "ModelCatalog":
        """Load and parse a model_providers.yaml file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        handles: List[ModelHandle] = []
        for entry in data.get("models", []):
            pricing_raw = entry["pricing"]
            pricing = ModelPricing(
                input_per_1m_usd=float(pricing_raw["input_per_1m_usd"]),
                output_per_1m_usd=float(pricing_raw["output_per_1m_usd"]),
                cached_input_per_1m_usd=(
                    float(pricing_raw["cached_input_per_1m_usd"])
                    if "cached_input_per_1m_usd" in pricing_raw
                    else None
                ),
            )
            handle = ModelHandle(
                provider_id=entry["provider_id"],
                model_id=entry["model_id"],
                tier=ModelTier(entry["tier"]),
                pricing=pricing,
                context_window=int(entry["context_window"]),
                capabilities=frozenset(entry.get("capabilities", [])),
                display_name=entry["display_name"],
                label=entry["label"],
            )
            handles.append(handle)

        return cls(handles)

    def get(self, provider_id: str, model_id: str) -> ModelHandle:
        """Return handle for a specific provider+model pair; raises KeyError if absent."""
        key = f"{provider_id}:{model_id}"
        if key not in self._index:
            raise KeyError(f"Model not found in catalog: {key}")
        return self._index[key]

    def get_by_key(self, key: str) -> ModelHandle:
        """Return handle by 'provider_id:model_id' key."""
        if key not in self._index:
            raise KeyError(f"Model not found in catalog: {key}")
        return self._index[key]

    def list_by_tier(self, tier: ModelTier) -> List[ModelHandle]:
        """All models for a given tier, preserving YAML order."""
        return [h for h in self._handles if h.tier == tier]

    def list_all(self) -> List[ModelHandle]:
        """All models in YAML order."""
        return list(self._handles)

    def list_by_provider(self, provider_id: str) -> List[ModelHandle]:
        """All models for a given provider_id, preserving YAML order."""
        return [h for h in self._handles if h.provider_id == provider_id]
