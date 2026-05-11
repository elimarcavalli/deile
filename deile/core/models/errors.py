"""Provider error types for the multi-provider router."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ProviderErrorEnvelope:
    """Structured representation of a provider API error."""

    provider_id: str
    model_id: str
    error_type: str          # "auth" | "rate_limit" | "invalid_request" | "context_length_exceeded" | "server" | "unknown"
    message: str
    http_status: Optional[int] = None
    raw_json: Dict[str, Any] = field(default_factory=dict)
    request_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)

    @property
    def is_context_length_exceeded(self) -> bool:
        return self.error_type == "context_length_exceeded"

    def to_display_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider_id,
            "model": self.model_id,
            "error_type": self.error_type,
            "http_status": self.http_status,
            "message": self.message,
            "request_id": self.request_id,
            "raw": self.raw_json,
        }


class ProviderInvocationError(Exception):
    """Raised when a provider call fails; carries the structured envelope."""

    def __init__(self, envelope: ProviderErrorEnvelope, *args: object) -> None:
        self.envelope = envelope
        super().__init__(envelope.message, *args)
