"""Testes unitários de ``deile.infrastructure.deile_monitor_client``.

Cobrem resolução de endpoint (default + override), resolução de token (env
e arquivo de secret), o erro ``MONITOR_AUTH_MISSING``, o happy-path de cada
método e o mapeamento de erros (400 do servidor, 404, timeout, unreachable).

Seguimos o estilo do repo (espelhando ``test_deile_worker_client.py``):
``httpx.MockTransport`` para respostas HTTP e ``monkeypatch`` para
env/arquivo/token. ``respx`` está disponível, mas nenhum teste do repo o usa
— manter o padrão ``MockTransport`` evita introduzir uma segunda convenção.
"""

from __future__ import annotations

import httpx
import pytest

from deile.infrastructure import deile_monitor_client as mc
from deile.infrastructure.deile_monitor_client import (
    MonitorClient,
    MonitorClientError,
    _read_token,
    _resolve_endpoint,
    _validate_token_charset,
)

pytestmark = pytest.mark.unit


# ----- endpoint resolution -----


def test_resolve_endpoint_default(monkeypatch):
    monkeypatch.delenv("DEILE_MONITOR_ENDPOINT", raising=False)
    assert _resolve_endpoint() == "http://deile-monitor:8769"


def test_resolve_endpoint_env_override(monkeypatch):
    monkeypatch.setenv("DEILE_MONITOR_ENDPOINT", "http://test.example:9000")
    assert _resolve_endpoint() == "http://test.example:9000"


def test_resolve_endpoint_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("DEILE_MONITOR_ENDPOINT", "http://m:8769/")
    assert _resolve_endpoint() == "http://m:8769"


# ----- token resolution -----


def test_read_token_from_env(monkeypatch):
    monkeypatch.setenv("DEILE_MONITOR_AUTH_TOKEN", " env-token-value ")
    assert _read_token() == "env-token-value"


def test_read_token_from_secret_file(monkeypatch, tmp_path):
    monkeypatch.delenv("DEILE_MONITOR_AUTH_TOKEN", raising=False)
    f1 = tmp_path / "missing"
    f2 = tmp_path / "present"
    f2.write_text("token-from-file\n", encoding="utf-8")
    monkeypatch.setattr(mc, "_TOKEN_FILES", (str(f1), str(f2)))
    assert _read_token() == "token-from-file"


def test_read_token_env_wins_over_file(monkeypatch, tmp_path):
    monkeypatch.setenv("DEILE_MONITOR_AUTH_TOKEN", "env-wins")
    f = tmp_path / "present"
    f.write_text("file-loses", encoding="utf-8")
    monkeypatch.setattr(mc, "_TOKEN_FILES", (str(f),))
    assert _read_token() == "env-wins"


def test_read_token_all_empty(monkeypatch):
    monkeypatch.delenv("DEILE_MONITOR_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(mc, "_TOKEN_FILES", ())
    assert _read_token() == ""


# ----- token charset validation -----


@pytest.mark.parametrize(
    "tok,ok",
    [
        ("ABCdef0123456789", True),
        ("token._-+/=:~with.special", True),
        ("abc\ndef-injection", False),  # LF -> header injection vector
        ("abc\rdef", False),  # CR
        ("abc\x00def", False),  # NUL
        ("short", False),  # below 16 char floor
        ("a" * 16, True),  # exact floor
    ],
)
def test_validate_token_charset(tok: str, ok: bool):
    assert _validate_token_charset(tok) is ok


# ----- shared transport harness -----


def _install_mock_transport(monkeypatch, handler) -> MonitorClient:
    """Instala MockTransport + token + endpoint e devolve um cliente fresco."""
    monkeypatch.setattr(mc, "_read_token", lambda: "a-valid-token-123")
    monkeypatch.setattr(mc, "_resolve_endpoint", lambda: "http://mock.invalid")
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return MonitorClient()


# ----- auth missing -----


async def test_get_status_auth_missing(monkeypatch):
    monkeypatch.setattr(mc, "_read_token", lambda: "")
    monkeypatch.setattr(mc, "_resolve_endpoint", lambda: "http://mock.invalid")
    cli = MonitorClient()
    with pytest.raises(MonitorClientError) as ei:
        await cli.get_status()
    assert ei.value.code == "MONITOR_AUTH_MISSING"


# ----- get_status -----


async def test_get_status_happy_path(monkeypatch):
    expected = {
        "last_tick": 42,
        "paused": False,
        "anomalies_total": 0,
        "known_anomalies": [],
        "now": 1717459200,
    }
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        assert request.method == "GET"
        return httpx.Response(200, json=expected)

    cli = _install_mock_transport(monkeypatch, handler)
    data = await cli.get_status()
    assert data == expected
    assert captured["url"].endswith("/v1/monitor-status")
    assert captured["headers"]["authorization"].startswith("Bearer ")
    assert captured["headers"]["x-request-id"]


# ----- post_command -----


async def test_post_command_happy_path(monkeypatch):
    captured = {}

    def handler(request):
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(
            200,
            json={"accepted": True, "command": "pause 30m", "effect": "ok"},
        )

    cli = _install_mock_transport(monkeypatch, handler)
    data = await cli.post_command("pause 30m")
    assert data["accepted"] is True
    assert captured["body"] == {"command": "pause 30m"}


async def test_post_command_400_unpacks_server_code(monkeypatch):
    def handler(request):
        return httpx.Response(
            400,
            json={"error": {"code": "BAD_COMMAND", "message": "unknown command"}},
        )

    cli = _install_mock_transport(monkeypatch, handler)
    with pytest.raises(MonitorClientError) as ei:
        await cli.post_command("nonsense")
    assert ei.value.code == "BAD_COMMAND"
    assert "unknown command" in ei.value.message


# ----- ask -----


async def test_ask_returns_request_id_on_202(monkeypatch):
    captured = {}

    def handler(request):
        import json as _json

        captured["body"] = _json.loads(request.content)
        return httpx.Response(202, json={"request_id": "abcd1234", "status": "running"})

    cli = _install_mock_transport(monkeypatch, handler)
    request_id = await cli.ask("qual o estado do pipeline?")
    assert request_id == "abcd1234"
    assert captured["body"] == {"question": "qual o estado do pipeline?"}


async def test_ask_missing_request_id_raises_bad_response(monkeypatch):
    def handler(request):
        return httpx.Response(202, json={"status": "running"})

    cli = _install_mock_transport(monkeypatch, handler)
    with pytest.raises(MonitorClientError) as ei:
        await cli.ask("x")
    assert ei.value.code == "MONITOR_BAD_RESPONSE"


# ----- get_ask_result -----


async def test_get_ask_result_happy_path(monkeypatch):
    expected = {"status": "done", "answer": "tudo verde"}

    def handler(request):
        assert "/v1/ask/req-1" in str(request.url)
        return httpx.Response(200, json=expected)

    cli = _install_mock_transport(monkeypatch, handler)
    data = await cli.get_ask_result("req-1")
    assert data == expected


async def test_get_ask_result_404_raises_not_found(monkeypatch):
    def handler(request):
        return httpx.Response(
            404, json={"error": {"code": "NOT_FOUND", "message": "unknown request_id"}}
        )

    cli = _install_mock_transport(monkeypatch, handler)
    with pytest.raises(MonitorClientError) as ei:
        await cli.get_ask_result("nope")
    assert ei.value.code == "MONITOR_NOT_FOUND"


# ----- error mapping -----


async def test_get_status_401_raises_auth_error(monkeypatch):
    def handler(request):
        return httpx.Response(
            401, json={"error": {"code": "UNAUTHORIZED", "message": "bad bearer"}}
        )

    cli = _install_mock_transport(monkeypatch, handler)
    with pytest.raises(MonitorClientError) as ei:
        await cli.get_status()
    assert ei.value.code == "MONITOR_AUTH_ERROR"


async def test_get_status_non_json_raises_bad_response(monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"not-json")

    cli = _install_mock_transport(monkeypatch, handler)
    with pytest.raises(MonitorClientError) as ei:
        await cli.get_status()
    assert ei.value.code == "MONITOR_BAD_RESPONSE"


async def test_get_status_http_error_no_envelope(monkeypatch):
    def handler(request):
        return httpx.Response(500, json={})

    cli = _install_mock_transport(monkeypatch, handler)
    with pytest.raises(MonitorClientError) as ei:
        await cli.get_status()
    assert ei.value.code == "MONITOR_HTTP_ERROR"


async def test_get_status_timeout(monkeypatch):
    def handler(request):
        raise httpx.TimeoutException("simulated timeout")

    cli = _install_mock_transport(monkeypatch, handler)
    with pytest.raises(MonitorClientError) as ei:
        await cli.get_status()
    assert ei.value.code == "MONITOR_TIMEOUT"


async def test_get_status_unreachable(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("nope")

    cli = _install_mock_transport(monkeypatch, handler)
    with pytest.raises(MonitorClientError) as ei:
        await cli.get_status()
    assert ei.value.code == "MONITOR_UNREACHABLE"
