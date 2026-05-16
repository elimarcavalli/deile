"""Unit tests for the shared provider error-mapping helpers.

Covers ``classify_http_error``, ``classify_provider_error`` and
``build_error_envelope`` — the provider-agnostic logic extracted from the
concrete Anthropic/OpenAI providers.
"""

from __future__ import annotations

import time

import pytest

from deile.core.models.error_mapping import (build_error_envelope,
                                             classify_http_error,
                                             classify_provider_error)
from deile.core.models.errors import ProviderErrorEnvelope


class _FakeExc(Exception):
    """Minimal stand-in for an Anthropic/OpenAI-style SDK exception."""

    def __init__(self, message="boom", *, status_code=None, body=None,
                 request_id=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.request_id = request_id
        self.response = response


def _anthropic_like(body, exc):
    """Body extractor matching the Anthropic top-level ``type``/``message`` layout."""
    del exc
    return str(body.get("type", "") or ""), str(body.get("message", "") or "")


# ---------------------------------------------------------------------------
# classify_http_error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,code,msg,expected", [
    (401, "", "", "auth"),
    (429, "", "", "rate_limit"),
    (500, "", "", "server"),
    (503, "", "", "server"),
    (400, "", "", "invalid_request"),
    (404, "", "", "invalid_request"),
    (400, "context_length_exceeded", "", "context_length_exceeded"),
    (400, "prompt_too_long", "", "context_length_exceeded"),
    (400, "", "the maximum context length is 8k tokens", "context_length_exceeded"),
    (413, "", "request exceeds the context window", "context_length_exceeded"),
    (None, "", "", "unknown"),
    (302, "", "", "unknown"),
])
def test_classify_http_error(status, code, msg, expected):
    assert classify_http_error(status, code, msg) == expected


def test_classify_http_error_extra_markers():
    assert classify_http_error(
        400, "", "prompt is too long", ("prompt is too long",)
    ) == "context_length_exceeded"
    # Without the provider-specific marker the same body is a plain 4xx.
    assert classify_http_error(400, "", "prompt is too long") == "invalid_request"


# ---------------------------------------------------------------------------
# classify_provider_error
# ---------------------------------------------------------------------------

def test_classify_provider_error_status_takes_precedence():
    exc = _FakeExc(status_code=401, body={"type": "anything"})
    assert classify_provider_error(exc, _anthropic_like) == "auth"


def test_classify_provider_error_dict_body_extractor():
    exc = _FakeExc(status_code=400,
                   body={"type": "context_length_exceeded", "message": "too big"})
    assert classify_provider_error(exc, _anthropic_like) == "context_length_exceeded"


def test_classify_provider_error_non_dict_body_falls_back_to_str():
    # body is not a dict -> err_code empty, err_msg = str(exc); the sniff
    # still finds the context-length marker in the exception message.
    exc = _FakeExc("the maximum context length was exceeded", status_code=400,
                   body="not-a-dict")
    assert classify_provider_error(exc, _anthropic_like) == "context_length_exceeded"


def test_classify_provider_error_without_status_is_unknown():
    exc = _FakeExc("connection reset")
    assert classify_provider_error(exc, _anthropic_like) == "unknown"


# ---------------------------------------------------------------------------
# build_error_envelope
# ---------------------------------------------------------------------------

def _always_auth(_exc):
    return "auth"


def test_build_error_envelope_dict_body():
    exc = _FakeExc("bad key", status_code=401, body={"error": "x"},
                   request_id="req_9")
    env = build_error_envelope(exc, "anthropic", "claude-opus-4-7", _always_auth)
    assert isinstance(env, ProviderErrorEnvelope)
    assert env.provider_id == "anthropic"
    assert env.model_id == "claude-opus-4-7"
    assert env.error_type == "auth"
    assert env.http_status == 401
    assert env.raw_json == {"error": "x"}
    assert env.request_id == "req_9"
    assert abs(env.timestamp - time.time()) < 5


def test_build_error_envelope_str_body_parsed_as_json():
    exc = _FakeExc(status_code=500, body='{"k": 1}')
    env = build_error_envelope(exc, "openai", "gpt", _always_auth)
    assert env.raw_json == {"k": 1}


def test_build_error_envelope_bytes_body_parsed_as_json():
    exc = _FakeExc(status_code=500, body=b'{"k": 2}')
    env = build_error_envelope(exc, "openai", "gpt", _always_auth)
    assert env.raw_json == {"k": 2}


def test_build_error_envelope_invalid_json_body_keeps_raw():
    exc = _FakeExc(status_code=500, body="<<not json>>")
    env = build_error_envelope(exc, "openai", "gpt", _always_auth)
    assert env.raw_json == {"raw_body": "<<not json>>"}


def test_build_error_envelope_request_id_from_response_headers():
    response = type("R", (), {"headers": {"request-id": "hdr-1"}})()
    exc = _FakeExc(status_code=429, body={}, response=response)
    env = build_error_envelope(exc, "openai", "gpt", _always_auth)
    assert env.request_id == "hdr-1"


def test_build_error_envelope_no_request_id_is_none():
    exc = _FakeExc(status_code=400, body={})
    env = build_error_envelope(exc, "openai", "gpt", _always_auth)
    assert env.request_id is None
