"""Unit tests for ``deile.infrastructure.deile_worker_client``.

Cover endpoint resolution, token resolution (env + 3 files in order),
charset validation, payload validation, and the six dispatch error
codes — all without touching the real network. We use
``httpx.MockTransport`` for HTTP responses and ``monkeypatch`` for
env/file resolution.
"""
from __future__ import annotations

import json

import httpx
import pytest

from deile.infrastructure import deile_worker_client as wc
from deile.infrastructure.deile_worker_client import (DEFAULT_TIMEOUT_S,
                                                      DeileWorkerClient,
                                                      DispatchPayload,
                                                      WorkerDispatchError,
                                                      _read_token,
                                                      _resolve_endpoint,
                                                      _validate_token_charset)

# ----- endpoint resolution -----

def test_resolve_endpoint_default(monkeypatch):
    monkeypatch.delenv("DEILE_WORKER_ENDPOINT", raising=False)
    assert _resolve_endpoint() == (
        "http://deile-worker.deile.svc.cluster.local:8766"
    )


def test_resolve_endpoint_env_override(monkeypatch):
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "http://test.example:9000")
    assert _resolve_endpoint() == "http://test.example:9000"


# ----- token resolution -----

def test_read_token_from_env(monkeypatch):
    monkeypatch.setenv("DEILE_WORKER_BEARER_TOKEN", " abc-token ")
    # Stripped.
    assert _read_token() == "abc-token"


def test_read_token_env_empty_falls_to_files(monkeypatch, tmp_path):
    monkeypatch.setenv("DEILE_WORKER_BEARER_TOKEN", "")
    # Point file list at our tmp_path; first file present wins.
    f1 = tmp_path / "auth1"
    f2 = tmp_path / "auth2"
    f1.write_text("token-from-f1\n", encoding="utf-8")
    f2.write_text("token-from-f2\n", encoding="utf-8")
    monkeypatch.setattr(wc, "_TOKEN_FILES", (str(f1), str(f2)))
    assert _read_token() == "token-from-f1"


def test_read_token_resolution_order(monkeypatch, tmp_path):
    monkeypatch.delenv("DEILE_WORKER_BEARER_TOKEN", raising=False)
    missing = tmp_path / "doesnotexist"
    present = tmp_path / "present"
    present.write_text("via-second-path", encoding="utf-8")
    monkeypatch.setattr(wc, "_TOKEN_FILES", (str(missing), str(present)))
    assert _read_token() == "via-second-path"


def test_read_token_all_empty(monkeypatch):
    monkeypatch.delenv("DEILE_WORKER_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(wc, "_TOKEN_FILES", ())
    assert _read_token() == ""


# ----- token charset validation -----

@pytest.mark.parametrize(
    "tok,ok",
    [
        ("ABCdef0123456789", True),
        ("token._-+/=:~with.special", True),
        ("abc\ndef", False),   # LF -> header injection vector
        ("abc\rdef", False),   # CR
        ("abc\x00def", False), # NUL
        ("abc def", False),    # space — not allowed in bearer charset here
        ("short", False),      # below 8 char floor
        ("a" * 8, True),       # exact floor
    ],
)
def test_validate_token_charset(tok: str, ok: bool):
    assert _validate_token_charset(tok) is ok


# ----- DispatchPayload validation -----

def test_payload_valid_minimum():
    p = DispatchPayload.model_validate(
        {"brief": "hello", "channel_id": "12345"}
    )
    assert p.persona == "developer"
    assert p.wait_for_result is True


def test_payload_rejects_blank_brief():
    with pytest.raises(Exception):
        DispatchPayload.model_validate({"brief": "   ", "channel_id": "x"})


def test_payload_rejects_unknown_persona():
    with pytest.raises(Exception):
        DispatchPayload.model_validate(
            {"brief": "b", "channel_id": "c", "persona": "evil"}
        )


def test_payload_strips_brief_whitespace():
    p = DispatchPayload.model_validate(
        {"brief": "  do stuff  ", "channel_id": "c"}
    )
    assert p.brief == "do stuff"


def test_payload_rejects_too_long_brief():
    with pytest.raises(Exception):
        DispatchPayload.model_validate(
            {"brief": "x" * 8001, "channel_id": "c"}
        )


def test_payload_accepts_attachments_and_user_message_id():
    p = DispatchPayload.model_validate({
        "brief": "b",
        "channel_id": "c",
        "user_message_id": "msg-1",
        "attachments": [{"url": "http://x"}],
    })
    body = p.model_dump(exclude_none=True)
    assert body["user_message_id"] == "msg-1"
    assert body["attachments"] == [{"url": "http://x"}]


# ----- dispatch error code coverage -----

def _good_payload() -> dict:
    return {"brief": "hello world", "channel_id": "12345"}


async def test_dispatch_auth_missing(monkeypatch):
    monkeypatch.setattr(wc, "_read_token", lambda: "")
    cli = DeileWorkerClient()
    with pytest.raises(WorkerDispatchError) as ei:
        await cli.dispatch(_good_payload(), wait=False)
    assert ei.value.error_code == "WORKER_AUTH_MISSING"


async def test_dispatch_auth_malformed_crlf(monkeypatch):
    monkeypatch.setattr(wc, "_read_token", lambda: "abc\ndef-injection")
    cli = DeileWorkerClient()
    with pytest.raises(WorkerDispatchError) as ei:
        await cli.dispatch(_good_payload(), wait=False)
    assert ei.value.error_code == "WORKER_AUTH_MALFORMED"


async def test_dispatch_transport_missing(monkeypatch):
    monkeypatch.setattr(wc, "_read_token", lambda: "a-valid-token-123")
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "httpx":
            raise ImportError("simulated")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    cli = DeileWorkerClient()
    with pytest.raises(WorkerDispatchError) as ei:
        await cli.dispatch(_good_payload(), wait=False)
    assert ei.value.error_code == "WORKER_TRANSPORT_MISSING"


async def test_dispatch_bad_request_invalid_payload(monkeypatch):
    monkeypatch.setattr(wc, "_read_token", lambda: "a-valid-token-123")
    cli = DeileWorkerClient()
    with pytest.raises(WorkerDispatchError) as ei:
        await cli.dispatch({"brief": "", "channel_id": "c"}, wait=False)
    assert ei.value.error_code == "BAD_REQUEST"


async def _run_with_transport(monkeypatch, handler):
    """Helper: install a MockTransport and a token, then dispatch."""
    monkeypatch.setattr(wc, "_read_token", lambda: "a-valid-token-123")
    monkeypatch.setattr(
        wc, "_resolve_endpoint", lambda: "http://mock.invalid"
    )
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    cli = DeileWorkerClient()
    return await cli.dispatch(_good_payload(), wait=False)


async def test_dispatch_timeout(monkeypatch):
    def handler(request):
        raise httpx.TimeoutException("simulated timeout")

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_with_transport(monkeypatch, handler)
    assert ei.value.error_code == "WORKER_TIMEOUT"


async def test_dispatch_unreachable(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("nope")

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_with_transport(monkeypatch, handler)
    assert ei.value.error_code == "WORKER_UNREACHABLE"


async def test_dispatch_bad_response_non_json(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"not-json-here")

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_with_transport(monkeypatch, handler)
    assert ei.value.error_code == "WORKER_BAD_RESPONSE"


async def test_dispatch_http_error_with_code(monkeypatch):
    def handler(request):
        return httpx.Response(
            500,
            json={"error": {"code": "CUSTOM_FAIL", "message": "boom"}},
        )

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_with_transport(monkeypatch, handler)
    assert ei.value.error_code == "CUSTOM_FAIL"


async def test_dispatch_http_error_no_code(monkeypatch):
    def handler(request):
        return httpx.Response(503, json={})

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_with_transport(monkeypatch, handler)
    assert ei.value.error_code == "WORKER_ERROR"


async def test_dispatch_success(monkeypatch):
    captured = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"ok": True, "task_id": "T-1", "files": ["a.py"], "elapsed_s": 1.2},
        )

    data = await _run_with_transport(monkeypatch, handler)
    assert data["task_id"] == "T-1"
    # Authorization header is set; X-Request-ID is propagated.
    assert captured["headers"]["authorization"].startswith("Bearer ")
    assert captured["headers"]["x-request-id"]
    assert captured["body"]["brief"] == "hello world"
    assert captured["body"]["persona"] == "developer"


async def test_dispatch_timeout_value_changes_with_wait(monkeypatch):
    """Sanity: timeout is float and reflects ``wait``."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(wc, "_read_token", lambda: "a-valid-token-123")
    monkeypatch.setattr(wc, "_resolve_endpoint", lambda: "http://mock.invalid")
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    captured_timeouts = []

    def _factory(*args, **kwargs):
        captured_timeouts.append(kwargs.get("timeout"))
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    cli = DeileWorkerClient()
    await cli.dispatch(_good_payload(), wait=False)
    await cli.dispatch(_good_payload(), wait=True)
    assert captured_timeouts[0] == 30.0
    assert captured_timeouts[1] == DEFAULT_TIMEOUT_S + 60.0


def test_default_timeout_is_float():
    assert isinstance(DEFAULT_TIMEOUT_S, float)
