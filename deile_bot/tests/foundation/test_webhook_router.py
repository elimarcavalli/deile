"""Tests for WebhookRouter."""

from __future__ import annotations

import pytest

from deile_bot.runtime.webhook_router import WebhookRouter


class TestRouter:
    async def test_register_and_dispatch(self):
        r = WebhookRouter()

        async def handler(payload):
            return {"echo": payload}

        r.register_object_handler("messages", handler)
        out = await r.dispatch("messages", {"a": 1})
        assert out["echo"] == {"a": 1}

    async def test_no_handler_returns_marker(self):
        r = WebhookRouter()
        out = await r.dispatch("unknown", {})
        assert out["status"] == "no_handler"

    async def test_handler_exception_caught(self):
        r = WebhookRouter()

        async def boom(payload):
            raise RuntimeError("x")

        r.register_object_handler("messages", boom)
        out = await r.dispatch("messages", {})
        assert out["status"] == "error"
        assert "RuntimeError" in out["error"]

    def test_duplicate_registration_raises(self):
        r = WebhookRouter()

        async def h(p):
            return {}

        r.register_object_handler("a", h)
        with pytest.raises(ValueError):
            r.register_object_handler("a", h)
