"""Tests para ModelRouter — fix #779 health-check timeout."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_router():
    from deile.core.models.router import ModelRouter

    router = ModelRouter.__new__(ModelRouter)
    router.providers = {}
    router._circuit_breaker_status = {}
    router._last_health_check = 0.0
    router.health_check_interval = 300.0
    return router


@pytest.mark.unit
class TestHealthCheckTimeout:
    """_health_check_if_needed marca unhealthy e não trava quando provider trava."""

    async def test_health_check_timeout_does_not_block_provider_selection(self):
        """AC-5a/5b: provider que nunca retorna → marcado unhealthy em < 35s."""
        import time

        router = _make_router()

        never_done = asyncio.Event()

        async def _hanging_health_check():
            await never_done.wait()
            return True

        slow_provider = MagicMock()
        slow_provider.health_check = _hanging_health_check
        router.providers["slow"] = slow_provider
        router._circuit_breaker_status["slow"] = False

        # Timeout bem pequeno para o teste ser rápido
        original_timeout = router._HEALTH_CHECK_TIMEOUT_S
        router._HEALTH_CHECK_TIMEOUT_S = 0.2

        t0 = time.monotonic()
        try:
            await router._health_check_if_needed()
        finally:
            router._HEALTH_CHECK_TIMEOUT_S = original_timeout
            never_done.set()

        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"health check deve completar rapidamente; demorou {elapsed:.2f}s"
        assert router._circuit_breaker_status.get("slow") is True, (
            "provider com timeout deve ser marcado como unhealthy"
        )

    async def test_timeout_error_distinct_from_regular_failure(self):
        """AC-5c: TimeoutError não confundido com failure genérico."""
        router = _make_router()

        log_calls = []

        never_done = asyncio.Event()

        async def _hanging():
            await never_done.wait()
            return True

        provider = MagicMock()
        provider.health_check = _hanging
        router.providers["p1"] = provider
        router._circuit_breaker_status["p1"] = False
        router._HEALTH_CHECK_TIMEOUT_S = 0.1

        with patch.object(
            router.__class__.__module__ and __import__("logging").getLogger("deile.core.models.router"),
            "warning",
            side_effect=lambda msg, *a, **kw: log_calls.append(msg),
        ):
            try:
                await router._health_check_if_needed()
            finally:
                never_done.set()

        # A mensagem deve mencionar timeout (não só "failed")
        assert any("timed out" in str(m).lower() or "timeout" in str(m).lower() for m in log_calls), (
            f"log deve mencionar timeout; calls: {log_calls}"
        )

    async def test_healthy_provider_resets_circuit_breaker(self):
        """Provider saudável reseta circuit breaker aberto."""
        router = _make_router()

        provider = MagicMock()
        provider.health_check = AsyncMock(return_value=True)
        router.providers["ok"] = provider
        router._circuit_breaker_status["ok"] = True  # já aberto

        await router._health_check_if_needed()

        assert router._circuit_breaker_status.get("ok") is False
