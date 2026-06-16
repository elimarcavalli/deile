"""Runtime configuration for a single provider."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ProviderConfig:
    """Resolved runtime config for one provider (loaded from model_providers.yaml)."""

    provider_id: str
    api_key_env: str  # env var name, e.g. "ANTHROPIC_API_KEY"
    base_url: Optional[
        str
    ]  # None for SDK default; "https://api.deepseek.com/v1" for DeepSeek
    sdk_kwargs: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    timeout_seconds: int = 120
    max_retries: int = 0  # 0 = router controls retries; SDK does not retry
