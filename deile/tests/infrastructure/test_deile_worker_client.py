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
from deile.infrastructure.deile_worker_client import (
    DEFAULT_TIMEOUT_S, DeileWorkerClient, DispatchPayload, WorkerDispatchError,
    _read_token, _resolve_endpoint, _validate_token_charset,
    reset_circuit_breaker, validate_dispatch_payload)


@pytest.fixture(autouse=True)
def _isolate_resilience(monkeypatch):
    """Isola a resiliência entre testes (issue #620 AC4/AC5).

    O circuit breaker é singleton de processo, então estado de um teste
    vazaria para o seguinte; resetamos antes de cada teste. O ``asyncio.sleep``
    do backoff é neutralizado para que os testes de caminho-de-falha não
    durmam de verdade (a contagem de tentativas é o que importa, não o
    tempo de parede; o cálculo real do backoff tem teste dedicado). Note
    que patchamos o sleep — não o ``_backoff_delay`` — para que o teste do
    backoff exercite a fórmula real.
    """
    reset_circuit_breaker()

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr(wc.asyncio, "sleep", _no_sleep)
    yield
    reset_circuit_breaker()


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
        ("short", False),      # below 16 char floor
        ("a" * 15, False),     # one below the floor
        ("a" * 16, True),      # exact floor — aligned with secrets_scanner
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


def test_payload_accepts_reviewer_persona():
    # The PR-review quality gate dispatches under the ``reviewer`` persona;
    # the wire contract must accept it (see implementer.WorkerImplementer.review).
    p = DispatchPayload.model_validate(
        {"brief": "b", "channel_id": "c", "persona": "reviewer"}
    )
    assert p.persona == "reviewer"


def test_payload_strips_brief_whitespace():
    p = DispatchPayload.model_validate(
        {"brief": "  do stuff  ", "channel_id": "c"}
    )
    assert p.brief == "do stuff"


def test_payload_rejects_too_long_brief():
    with pytest.raises(Exception):
        DispatchPayload.model_validate(
            {"brief": "x" * 200_001, "channel_id": "c"}
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


# ----- validate_dispatch_payload -----

def test_validate_dispatch_payload_valid_returns_model():
    p = validate_dispatch_payload({"brief": "hello", "channel_id": "12345"})
    assert isinstance(p, DispatchPayload)
    assert p.brief == "hello"


def test_validate_dispatch_payload_invalid_raises_bad_request():
    with pytest.raises(WorkerDispatchError) as ei:
        validate_dispatch_payload({"brief": "", "channel_id": "c"})
    assert ei.value.error_code == "BAD_REQUEST"


def test_validate_dispatch_payload_does_not_leak_input_values():
    # The brief is untrusted (Discord) content and may carry PII — the
    # rejection message must describe the failing field WITHOUT echoing the
    # offending value (pilar 08).
    secret = "SUPER-SECRET-PII-abc123"
    with pytest.raises(WorkerDispatchError) as ei:
        validate_dispatch_payload(
            {"brief": secret, "channel_id": "c", "persona": "evil"}
        )
    assert ei.value.error_code == "BAD_REQUEST"
    assert secret not in ei.value.message
    assert "persona" in ei.value.message


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


def _install_mock_transport(monkeypatch, handler) -> DeileWorkerClient:
    """Install MockTransport + token + endpoint and return a fresh client.

    Shared bootstrap for the dispatch / get_progress / get_result helpers —
    all three paths needed the same four monkeypatch lines (token, endpoint,
    AsyncClient factory, transport).
    """
    monkeypatch.setattr(wc, "_read_token", lambda: "a-valid-token-123")
    monkeypatch.setattr(wc, "_resolve_endpoint", lambda: "http://mock.invalid")
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return DeileWorkerClient()


async def _run_with_transport(monkeypatch, handler):
    """Helper: install a MockTransport and a token, then dispatch."""
    cli = _install_mock_transport(monkeypatch, handler)
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
    """Structured ``httpx.Timeout``: read/write follow ``wait`` budget, but
    connect/pool are capped tight so a hung connect can't freeze the tick.

    Regressão de produção 2026-06-01: ``httpx.AsyncClient(timeout=<float>)``
    aplicava o budget de 2h também ao connect, deixando um socket pendurado
    travar o tick por 2h. O fix passa um ``httpx.Timeout`` estruturado.
    """
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

    nowait_t, wait_t = captured_timeouts
    # Ambos são ``httpx.Timeout`` estruturados (NÃO floats escalares).
    assert isinstance(nowait_t, httpx.Timeout)
    assert isinstance(wait_t, httpx.Timeout)
    # nowait: budget curto (30s) em todas as fases.
    assert nowait_t.read == 30.0
    assert nowait_t.connect == 30.0
    # wait: read/write seguem o budget do task (2h+); connect/pool ficam curtos
    # — é exatamente isto que impede um connect pendurado de travar o tick.
    assert wait_t.read == DEFAULT_TIMEOUT_S + 60.0
    assert wait_t.write == DEFAULT_TIMEOUT_S + 60.0
    assert wait_t.connect == 30.0
    assert wait_t.pool == 30.0


def test_default_timeout_is_float():
    assert isinstance(DEFAULT_TIMEOUT_S, float)


# ----- get_progress / get_result (issue #257) -----

async def _run_get_with_transport(monkeypatch, handler, *, fn, path):
    """Helper: install MockTransport and exercise ``cli.<fn>(task_id)``.

    ``path`` is unused but kept as a kwarg for callers that document intent
    (it appears in the request URL the handler inspects).
    """
    del path  # documented intent only — actual URL is asserted by handlers
    cli = _install_mock_transport(monkeypatch, handler)
    method = getattr(cli, fn)
    return await method("test-task-id")


async def test_get_progress_returns_snapshot_dict(monkeypatch):
    expected = {
        "task_id": "test-task-id",
        "ok": None,
        "phase": "▶️ trabalhando...",
        "progress_lines": ["a", "b"],
        "elapsed_s": 2.5,
    }

    def handler(request):
        assert request.method == "GET"
        assert "/v1/progress/test-task-id" in str(request.url)
        assert request.headers["authorization"].startswith("Bearer ")
        return httpx.Response(200, json=expected)

    data = await _run_get_with_transport(
        monkeypatch, handler, fn="get_progress", path="/v1/progress"
    )
    assert data == expected


async def test_get_progress_404_raises_not_found(monkeypatch):
    def handler(request):
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "x"}}
        )

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_get_with_transport(
            monkeypatch, handler, fn="get_progress", path="/v1/progress"
        )
    assert ei.value.error_code == "NOT_FOUND"


async def test_get_progress_auth_missing(monkeypatch):
    monkeypatch.setattr(wc, "_read_token", lambda: "")
    monkeypatch.setattr(wc, "_resolve_endpoint", lambda: "http://mock.invalid")
    cli = DeileWorkerClient()
    with pytest.raises(WorkerDispatchError) as ei:
        await cli.get_progress("any-task")
    assert ei.value.error_code == "WORKER_AUTH_MISSING"


async def test_get_result_returns_full_dict(monkeypatch):
    expected = {
        "task_id": "test-task-id",
        "ok": True,
        "files": ["foo.py"],
        "summary": "done",
        "elapsed_s": 12.0,
    }

    def handler(request):
        assert "/v1/result/test-task-id" in str(request.url)
        return httpx.Response(200, json=expected)

    data = await _run_get_with_transport(
        monkeypatch, handler, fn="get_result", path="/v1/result"
    )
    assert data == expected


async def test_get_result_timeout(monkeypatch):
    def handler(request):
        raise httpx.TimeoutException("simulated")

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_get_with_transport(
            monkeypatch, handler, fn="get_result", path="/v1/result"
        )
    assert ei.value.error_code == "WORKER_TIMEOUT"


async def test_get_progress_non_dict_body(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b'"not-a-dict"', headers={"Content-Type": "application/json"})

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_get_with_transport(
            monkeypatch, handler, fn="get_progress", path="/v1/progress"
        )
    assert ei.value.error_code == "WORKER_BAD_RESPONSE"


# ----- AC4: retry com exponential backoff (issue #620) --------------------
#
# A FIAÇÃO real (cliente → MockTransport) é exercitada contando os hits do
# handler: a função de dispatch tem de re-tentar APENAS falhas transientes
# (timeout/unreachable/5xx) e NUNCA 4xx (incl. 409/429). O backoff já é
# zerado pelo fixture autouse ``_isolate_resilience`` (sem sleeps reais);
# o timing fica no teste dedicado de fake clock.


async def test_retry_worker_timeout_three_attempts(monkeypatch):
    """WORKER_TIMEOUT é transiente → 3 tentativas antes de desistir (AC4)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.TimeoutException("simulated")

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_with_transport(monkeypatch, handler)
    assert ei.value.error_code == "WORKER_TIMEOUT"
    assert calls["n"] == 3


async def test_retry_unreachable_three_attempts(monkeypatch):
    """WORKER_UNREACHABLE também é transiente → 3 tentativas (AC4)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("nope")

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_with_transport(monkeypatch, handler)
    assert ei.value.error_code == "WORKER_UNREACHABLE"
    assert calls["n"] == 3


async def test_retry_http_500_three_attempts(monkeypatch):
    """HTTP 500 (>= 500) é transiente → 3 tentativas (AC4)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(500, json={"error": {"code": "BOOM", "message": "x"}})

    with pytest.raises(WorkerDispatchError):
        await _run_with_transport(monkeypatch, handler)
    assert calls["n"] == 3


async def test_no_retry_http_409(monkeypatch):
    """HTTP 409 (duplicate in-flight) é 4xx → 0 retry, falha na 1ª (AC4)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            409, json={"error": {"code": "duplicate_in_flight", "message": "x"}}
        )

    with pytest.raises(WorkerDispatchError) as ei:
        await _run_with_transport(monkeypatch, handler)
    assert ei.value.error_code == "duplicate_in_flight"
    assert calls["n"] == 1


async def test_no_retry_http_429(monkeypatch):
    """HTTP 429 (rate-limited) é 4xx → 0 retry (AC4)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            429, json={"error": {"code": "rate_limited", "message": "slow down"}}
        )

    with pytest.raises(WorkerDispatchError):
        await _run_with_transport(monkeypatch, handler)
    assert calls["n"] == 1


async def test_retry_succeeds_on_second_attempt(monkeypatch):
    """Falha transiente seguida de sucesso → retorna o sucesso (AC4)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("first fails")
        return httpx.Response(200, json={"ok": True, "task_id": "T-2"})

    data = await _run_with_transport(monkeypatch, handler)
    assert data["task_id"] == "T-2"
    assert calls["n"] == 2


async def test_max_retries_payload_caps_attempts(monkeypatch):
    """``max_retries=0`` no payload força 1 tentativa só, mesmo em 5xx (AC4)."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={})

    cli = _install_mock_transport(monkeypatch, handler)
    payload = {**_good_payload(), "max_retries": 0}
    with pytest.raises(WorkerDispatchError):
        await cli.dispatch(payload, wait=False)
    assert calls["n"] == 1


def test_backoff_delay_grows_exponentially_with_jitter():
    """Backoff base=1s, factor=2, jitter=±0.3s para retry_index 0/1/2 (AC4)."""
    # retry_index 0 → ~1s (±0.3), 1 → ~2s (±0.3), 2 → ~4s (±0.3).
    for retry_index, center in ((0, 1.0), (1, 2.0), (2, 4.0)):
        for _ in range(50):
            d = DeileWorkerClient._backoff_delay(retry_index)
            assert center - 0.3 <= d <= center + 0.3
            assert d >= 0.0
