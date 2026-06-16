"""Tests for the observability HTTP client (issue #347).

The client is a thin layer over ``aiohttp`` whose purpose is to never
crash the panel renderer.  These tests do not exercise the network — they
verify the dataclass surface, URL composition, and the fallback path
when ``aiohttp`` errors are raised by stubbed handlers.
"""

from __future__ import annotations

from deile.ui.panel.observability.client import (
    ApiError,
    ClaudeWorkerSessionsClient,
    ClusterObservabilityClient,
    PipelineStatusClient,
)


def test_pipeline_status_client_strips_trailing_slash():
    """``base_url`` is normalized so callers don't double-slash paths."""
    c = PipelineStatusClient("http://pipeline:8768/", bearer_token="t")
    assert c.base_url == "http://pipeline:8768"


async def test_claude_worker_client_builds_kill_confirm_token():
    """``kill`` derives the confirm token from the first 8 hex chars."""
    c = ClaudeWorkerSessionsClient("http://w:8767")
    # Sanity check the helper used to build the body.  We exercise the
    # method via a recorder by stubbing ``post`` directly — no network.
    captured = {}

    async def fake_post(path, *, json_body=None):
        captured["path"] = path
        captured["json"] = json_body
        return {"killed": True}

    c.post = fake_post  # type: ignore[assignment]

    result = await c.kill("abcdef1234567890")
    assert captured["path"] == "/v1/sessions/abcdef1234567890/kill"
    assert captured["json"] == {"confirm": "yes-task-abcdef12"}
    assert result == {"killed": True}


def test_cluster_observability_client_factory():
    """``from_endpoints`` builds both sub-clients with the right URLs."""
    c = ClusterObservabilityClient.from_endpoints(
        pipeline_url="http://pipeline:8768",
        pipeline_token="pt",
        claude_worker_url="http://claude-worker:8767",
        claude_worker_token="ct",
    )
    assert c.pipeline.base_url == "http://pipeline:8768"
    assert c.pipeline.bearer_token == "pt"
    assert c.claude_worker.base_url == "http://claude-worker:8767"
    assert c.claude_worker.bearer_token == "ct"


def test_api_error_dataclass_fields():
    """``ApiError`` carries status, message, optional detail dict."""
    err = ApiError(status=502, message="bad gateway", detail={"x": 1})
    assert err.status == 502
    assert err.message == "bad gateway"
    assert err.detail == {"x": 1}


async def test_request_swallows_connection_errors(monkeypatch):
    """A connection error becomes an :class:`ApiError`, not a raise.

    We monkeypatch ``aiohttp.ClientSession.request`` to raise so we don't
    touch the network — the contract is that the panel can render the
    error in a footer.
    """
    import aiohttp

    class _BoomSession:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise ConnectionRefusedError("nope")

        async def __aexit__(self, *exc):
            return False

        async def close(self):
            return None

        def request(self, *a, **kw):
            return self

    monkeypatch.setattr(aiohttp, "ClientSession", _BoomSession)
    c = PipelineStatusClient("http://x:1", bearer_token="t")
    reply = await c.get_status()
    assert isinstance(reply, ApiError)
    assert reply.status == 0
    assert "nope" in reply.message
