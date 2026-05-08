"""Tests: router error exposure (errors_by_handle) + observability — Phase 14."""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.core.exceptions import ModelError
from deile.core.models.errors import (ProviderErrorEnvelope,
                                      ProviderInvocationError)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_envelope(provider_id: str, http_status: int = 401) -> ProviderErrorEnvelope:
    return ProviderErrorEnvelope(
        provider_id=provider_id,
        model_id="test-model",
        error_type="auth",
        message=f"{provider_id} auth failed",
        http_status=http_status,
        raw_json={"error": {"type": "authentication_error"}, "status": http_status},
        request_id=f"req-{provider_id}",
        timestamp=time.time(),
    )


def _make_invocation_error(provider_id: str, http_status: int = 401) -> ProviderInvocationError:
    return ProviderInvocationError(_make_envelope(provider_id, http_status))


# ---------------------------------------------------------------------------
# ModelError — errors_by_handle in context
# ---------------------------------------------------------------------------

class TestModelErrorContext:
    def test_model_error_carries_context(self):
        _make_envelope("anthropic")
        errors = {"anthropic": {"error_type": "auth", "http_status": 401, "raw_json": {}}}
        err = ModelError(
            "ALL_TIER_PROVIDERS_FAILED",
            error_code="ALL_TIER_PROVIDERS_FAILED",
            context={"errors_by_handle": errors},
        )
        assert "errors_by_handle" in err.context
        assert "anthropic" in err.context["errors_by_handle"]

    def test_model_error_raw_json_preserved(self):
        raw = {"error": {"type": "authentication_error"}, "status": 401}
        errors = {"anthropic": {"error_type": "auth", "http_status": 401, "raw_json": raw}}
        err = ModelError(
            "all failed",
            error_code="ALL_TIER_PROVIDERS_FAILED",
            context={"errors_by_handle": errors},
        )
        extracted = err.context["errors_by_handle"]["anthropic"]["raw_json"]
        assert extracted["status"] == 401


# ---------------------------------------------------------------------------
# execute_with_fallback — raises ModelError with errors_by_handle
# ---------------------------------------------------------------------------

class TestExecuteWithFallbackErrorExposure:
    def _make_router(self):
        from deile.core.models.router import ModelRouter
        return ModelRouter()

    @pytest.mark.asyncio
    async def test_single_provider_auth_failure_exposes_envelope(self):
        router = self._make_router()
        failing_provider = MagicMock()
        failing_provider.provider_id = "anthropic"
        failing_provider.provider_name = "anthropic"
        failing_provider.model_name = "claude-opus-4-7"
        failing_provider.generate = AsyncMock(
            side_effect=_make_invocation_error("anthropic")
        )
        router.register_provider(failing_provider)

        with pytest.raises(ModelError) as exc_info:
            await router.execute_with_fallback([], max_retries=1)

        err = exc_info.value
        assert err.error_code == "ALL_TIER_PROVIDERS_FAILED"
        assert "errors_by_handle" in err.context

    @pytest.mark.asyncio
    async def test_successful_call_does_not_raise(self):
        router = self._make_router()
        good_provider = MagicMock()
        good_provider.provider_id = "anthropic"
        good_provider.provider_name = "anthropic"
        good_provider.model_name = "claude-opus-4-7"
        from deile.core.models.base import ModelResponse, ModelUsage
        good_provider.generate = AsyncMock(
            return_value=ModelResponse(
                content="ok",
                model_name="claude-opus-4-7",
                usage=ModelUsage(),
            )
        )
        router.register_provider(good_provider)

        result = await router.execute_with_fallback([], max_retries=1)
        assert result.content == "ok"


# ---------------------------------------------------------------------------
# ProviderErrorEnvelope — raw_json is JSON-serialisable
# ---------------------------------------------------------------------------

class TestProviderErrorEnvelopeSerialisation:
    def test_envelope_raw_json_is_serialisable(self):
        env = _make_envelope("openai", http_status=429)
        serialised = json.dumps(env.raw_json)
        decoded = json.loads(serialised)
        assert decoded["status"] == 429

    def test_envelope_fields(self):
        env = _make_envelope("deepseek")
        assert env.provider_id == "deepseek"
        assert env.error_type == "auth"
        assert env.http_status == 401

    def test_invocation_error_carries_envelope(self):
        exc = _make_invocation_error("anthropic")
        assert exc.envelope.provider_id == "anthropic"
        assert exc.envelope.http_status == 401


# ---------------------------------------------------------------------------
# log_router_event — writes JSONL
# ---------------------------------------------------------------------------

class TestLogRouterEvent:
    @pytest.mark.asyncio
    async def test_log_router_event_writes_jsonl(self, tmp_path):
        import deile.storage.debug_logger as _mod
        from deile.storage.debug_logger import _DebugLogger

        logger = _DebugLogger()
        events_file = tmp_path / "router_events.jsonl"

        original = _mod._EVENTS_LOG
        _mod._EVENTS_LOG = events_file
        try:
            await logger.log_router_event(
                "provider_selected",
                {"provider_id": "anthropic", "tier": "tier_1"},
            )
        finally:
            _mod._EVENTS_LOG = original

        assert events_file.exists()
        lines = events_file.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "provider_selected"
        assert record["provider_id"] == "anthropic"
        assert "ts" in record

    @pytest.mark.asyncio
    async def test_log_multiple_events(self, tmp_path):
        import deile.storage.debug_logger as _mod
        from deile.storage.debug_logger import _DebugLogger

        logger = _DebugLogger()
        events_file = tmp_path / "router_events.jsonl"
        original = _mod._EVENTS_LOG
        _mod._EVENTS_LOG = events_file
        try:
            await logger.log_router_event("cascade_fallback", {"from": "anthropic", "to": "openai"})
            await logger.log_router_event("circuit_breaker_opened", {"provider_id": "anthropic"})
        finally:
            _mod._EVENTS_LOG = original

        lines = events_file.read_text().strip().splitlines()
        assert len(lines) == 2
        events = [json.loads(line)["event"] for line in lines]
        assert events == ["cascade_fallback", "circuit_breaker_opened"]

    @pytest.mark.asyncio
    async def test_log_router_event_all_valid_types(self, tmp_path):
        import deile.storage.debug_logger as _mod
        from deile.storage.debug_logger import _DebugLogger

        logger = _DebugLogger()
        events_file = tmp_path / "router_events.jsonl"
        original = _mod._EVENTS_LOG
        _mod._EVENTS_LOG = events_file
        valid_types = [
            "provider_selected",
            "cascade_fallback",
            "circuit_breaker_opened",
            "circuit_breaker_closed",
            "budget_exceeded",
        ]
        try:
            for evt in valid_types:
                await logger.log_router_event(evt, {"detail": evt})
        finally:
            _mod._EVENTS_LOG = original

        lines = events_file.read_text().strip().splitlines()
        assert len(lines) == len(valid_types)
