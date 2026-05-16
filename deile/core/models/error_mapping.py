"""Shared error-envelope builder for concrete LLM providers.

Provider-agnostic: must NOT import any external SDK. Concrete providers
(``anthropic_provider``, ``openai_provider``, …) pass a ``classify_fn``
callable that maps their own exception type to an ``error_type`` string.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict

from deile.core.models.errors import ProviderErrorEnvelope


def build_error_envelope(
    exc: Exception,
    provider_id: str,
    model_id: str,
    classify_fn: Callable[[Exception], str],
) -> ProviderErrorEnvelope:
    """Build a :class:`ProviderErrorEnvelope` from a provider SDK exception.

    The common work — extracting the HTTP status, parsing the response body
    (``dict``/``bytes``/``str`` → JSON) and resolving the request id — lives
    here. ``classify_fn`` receives the raw exception and returns the
    provider-specific ``error_type`` classification.
    """
    status = getattr(exc, "status_code", None)
    raw: Dict[str, Any] = {}
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        raw = body
    elif isinstance(body, (str, bytes)):
        try:
            raw = json.loads(body)
        except Exception:
            raw = {"raw_body": str(body)}

    request_id = getattr(exc, "request_id", None)
    if request_id is None:
        response = getattr(exc, "response", None)
        if response is not None:
            request_id = getattr(response, "headers", {}).get("request-id")

    return ProviderErrorEnvelope(
        provider_id=provider_id,
        model_id=model_id,
        error_type=classify_fn(exc),
        message=str(exc),
        http_status=status,
        raw_json=raw,
        request_id=str(request_id) if request_id else None,
        timestamp=time.time(),
    )
