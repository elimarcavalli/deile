"""Tests: ProviderErrorEnvelope serialization — Phase 1."""

import time

import pytest

from deile.core.models.errors import ProviderErrorEnvelope, ProviderInvocationError


@pytest.fixture
def envelope():
    return ProviderErrorEnvelope(
        provider_id="anthropic",
        model_id="claude-opus-4-7",
        error_type="auth",
        message="Invalid API key",
        http_status=401,
        raw_json={"error": {"type": "authentication_error", "message": "Invalid API key"}},
        request_id="req_123",
        timestamp=1_700_000_000.0,
    )


def test_envelope_to_display_dict(envelope):
    d = envelope.to_display_dict()
    assert d["provider"] == "anthropic"
    assert d["model"] == "claude-opus-4-7"
    assert d["error_type"] == "auth"
    assert d["http_status"] == 401
    assert d["request_id"] == "req_123"
    assert "raw" in d


def test_envelope_raw_json_preserved(envelope):
    assert envelope.raw_json["error"]["type"] == "authentication_error"


def test_provider_invocation_error_carries_envelope(envelope):
    exc = ProviderInvocationError(envelope)
    assert exc.envelope is envelope
    assert "Invalid API key" in str(exc)


def test_envelope_default_timestamp_is_recent():
    env = ProviderErrorEnvelope(
        provider_id="openai",
        model_id="gpt-4o",
        error_type="server",
        message="Internal server error",
    )
    assert abs(env.timestamp - time.time()) < 5


def test_envelope_optional_fields_default_none():
    env = ProviderErrorEnvelope(
        provider_id="deepseek",
        model_id="deepseek-chat",
        error_type="unknown",
        message="Mystery error",
    )
    assert env.http_status is None
    assert env.request_id is None
    assert env.raw_json == {}
