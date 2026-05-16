"""Tests for GeminiProvider typed-error handling.

Covers the bug fix that made Gemini emit a typed ``ProviderErrorEnvelope``
(matching the Anthropic/OpenAI contract) instead of an attribute-less dict, so
``ToolLoopExecutor`` can read ``envelope.error_type``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from deile.core.models.errors import (ProviderErrorEnvelope,
                                      ProviderInvocationError)
from deile.core.models.gemini_provider import (_classify_gemini_error,
                                               _make_envelope)


class _FakeAPIError(Exception):
    """Stand-in for a ``google.genai.errors.APIError`` (HTTP status under
    ``code``, coarse string under ``status``, human text under ``message``)."""

    def __init__(self, *, code=None, status="", message="", details=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.details = details


@pytest.fixture
def gemini_provider(monkeypatch):
    """Construct a GeminiProvider without hitting the network."""
    from deile.core.models.catalog import ModelHandle, ModelPricing
    from deile.core.models.gemini_provider import GeminiProvider
    from deile.core.models.provider_config import ProviderConfig
    from deile.core.models.tier import ModelTier

    handle = ModelHandle(
        provider_id="gemini",
        model_id="gemini-2.5-pro",
        tier=ModelTier.TIER_1,
        pricing=ModelPricing(input_per_1m_usd=1.25, output_per_1m_usd=5.0),
        context_window=2_000_000,
        capabilities=frozenset({"function_calling", "vision"}),
        display_name="Gemini 2.5 Pro",
        label="flagship",
    )
    cfg = ProviderConfig(
        provider_id="gemini", api_key_env="GOOGLE_API_KEY", base_url=None
    )
    monkeypatch.setenv("GOOGLE_API_KEY", "fake")
    with patch("deile.core.models.gemini_provider.genai"):
        provider = GeminiProvider(handle, cfg)
    return provider


# ---------------------------------------------------------------------------
# _classify_gemini_error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("code,status,message,expected", [
    (429, "RESOURCE_EXHAUSTED", "", "rate_limit"),
    (401, "UNAUTHENTICATED", "", "auth"),
    (400, "INVALID_ARGUMENT", "", "invalid_request"),
    (400, "", "input token count exceeds the maximum context length", "context_length_exceeded"),
    (503, "UNAVAILABLE", "", "server"),
    (None, "", "weird transport failure", "unknown"),
])
def test_classify_gemini_error(code, status, message, expected):
    exc = _FakeAPIError(code=code, status=status, message=message)
    assert _classify_gemini_error(exc) == expected


def test_classify_gemini_error_non_int_code_is_unknown():
    # A non-int ``code`` (e.g. a string) cannot be an HTTP status -> unknown.
    assert _classify_gemini_error(_FakeAPIError(code="429")) == "unknown"


# ---------------------------------------------------------------------------
# _make_envelope
# ---------------------------------------------------------------------------

def test_make_envelope_basic():
    exc = _FakeAPIError(code=429, status="RESOURCE_EXHAUSTED",
                        message="quota exceeded", details={"reason": "quota"})
    env = _make_envelope(exc, "gemini", "gemini-2.5-pro")

    assert isinstance(env, ProviderErrorEnvelope)
    assert env.provider_id == "gemini"
    assert env.model_id == "gemini-2.5-pro"
    assert env.error_type == "rate_limit"
    assert env.http_status == 429
    assert env.message == "quota exceeded"
    assert env.raw_json == {"reason": "quota"}


def test_make_envelope_non_int_code_and_no_details():
    env = _make_envelope(_FakeAPIError(message="boom"), "gemini", "m")
    assert env.http_status is None
    assert env.raw_json == {}
    assert env.error_type == "unknown"


def test_make_envelope_message_falls_back_to_str():
    exc = _FakeAPIError(code=500)
    exc.args = ("fallback text",)
    env = _make_envelope(exc, "gemini", "m")
    assert env.message == "fallback text"


# ---------------------------------------------------------------------------
# generate() — wraps failures into the typed ProviderInvocationError contract
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_wraps_api_error_into_typed_envelope(gemini_provider):
    from google.genai import errors as genai_errors

    from deile.core.models.base import ModelMessage

    api_err = genai_errors.APIError.__new__(genai_errors.APIError)
    api_err.code = 429
    api_err.status = "RESOURCE_EXHAUSTED"
    api_err.message = "quota exceeded"
    api_err.details = {"reason": "quota"}

    gemini_provider._generate_with_new_sdk = AsyncMock(side_effect=api_err)

    with pytest.raises(ProviderInvocationError) as exc_info:
        await gemini_provider.generate([ModelMessage(role="user", content="hi")])

    envelope = exc_info.value.envelope
    assert envelope.error_type == "rate_limit"
    assert envelope.http_status == 429
    assert envelope.provider_id == gemini_provider.provider_id


@pytest.mark.asyncio
async def test_generate_wraps_non_api_error_as_unknown(gemini_provider):
    from deile.core.models.base import ModelMessage

    gemini_provider._generate_with_new_sdk = AsyncMock(
        side_effect=RuntimeError("serialization boom")
    )

    with pytest.raises(ProviderInvocationError) as exc_info:
        await gemini_provider.generate([ModelMessage(role="user", content="hi")])

    assert exc_info.value.envelope.error_type == "unknown"
