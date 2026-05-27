"""Tests para ``DeileWorkerClient.dispatch(endpoint_url=...)`` (issue #309 fase 2).

Cobertura do **fix do concern de produção** da Task 4: o ``WorkerImplementer``
plumba ``_resolve_endpoint(stage)`` e passa a URL via ``_post_dispatch``, mas
o ``DeileWorkerClient.dispatch`` real continuava postando em
``DEILE_WORKER_ENDPOINT`` por env var — ignorando a URL resolvida. Esses
testes garantem que o kwarg ``endpoint_url`` é honrado e que, na ausência
dele, o fallback para a env var (comportamento legacy) permanece intacto.

Usamos ``httpx.MockTransport`` para capturar o ``request.url`` real — a única
verificação autoritativa, já que tudo entre o ``dispatch`` e o wire é
implementação interna do ``httpx.AsyncClient``.
"""
from __future__ import annotations

import httpx

from deile.infrastructure import deile_worker_client as wc
from deile.infrastructure.deile_worker_client import DeileWorkerClient


def _good_payload() -> dict:
    return {"brief": "hello world", "channel_id": "12345"}


def _install_capture_transport(
    monkeypatch, captured: list[str], status: int = 200, body: dict | None = None,
) -> DeileWorkerClient:
    """Instala MockTransport que captura a URL e devolve 200 OK por default.

    Diferente do helper em ``test_deile_worker_client.py``, aqui NÃO patchamos
    ``_resolve_endpoint`` no módulo — queremos que o cliente USE o
    ``endpoint_url`` quando passado, ou caia na env var (que setamos via
    ``monkeypatch.setenv``) quando não passado. Esse é o ponto exato do bug
    que estamos cobrindo.
    """
    monkeypatch.setattr(wc, "_read_token", lambda: "a-valid-token-123")
    resp_body = body if body is not None else {"ok": True, "task_id": "T-1"}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return httpx.Response(status, json=resp_body)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)
    return DeileWorkerClient()


async def test_dispatch_uses_endpoint_url_when_provided(monkeypatch):
    """``endpoint_url`` kwarg sobrescreve o endpoint default do client."""
    # Env var aponta para um destino DIFERENTE do que queremos — esse é o
    # cenário real: ``DEILE_WORKER_ENDPOINT`` é o deile-worker default, mas o
    # ``WorkerImplementer._resolve_endpoint("implement")`` resolveu para o
    # claude-worker, e queremos que ESSE seja o destino HTTP.
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "http://default-worker:8766")

    captured: list[str] = []
    cli = _install_capture_transport(monkeypatch, captured)
    await cli.dispatch(
        _good_payload(), wait=False,
        endpoint_url="http://claude-worker:8767",
    )

    assert captured, "no request captured by mock transport"
    assert any("claude-worker:8767" in url for url in captured), (
        f"endpoint_url ignored; URLs called: {captured}"
    )
    # Negative assertion: a env var NÃO deve ter sido usada quando o
    # ``endpoint_url`` foi explicitamente passado — esse é o bug a evitar.
    assert not any("default-worker:8766" in url for url in captured), (
        f"env var leaked through endpoint_url override: {captured}"
    )


async def test_dispatch_falls_back_to_env_when_no_endpoint_url(monkeypatch):
    """Sem ``endpoint_url``, usa ``DEILE_WORKER_ENDPOINT`` (legacy backward compat)."""
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "http://envvar-worker:8766")

    captured: list[str] = []
    cli = _install_capture_transport(monkeypatch, captured)
    await cli.dispatch(_good_payload(), wait=False)  # NO endpoint_url

    assert captured, "no request captured by mock transport"
    assert any("envvar-worker:8766" in url for url in captured), (
        f"env fallback broken; URLs: {captured}"
    )


async def test_dispatch_endpoint_url_strips_trailing_slash(monkeypatch):
    """``endpoint_url`` com barra final NÃO deve duplicar a barra no path.

    O comportamento histórico (``_resolve_endpoint`` interno) faz
    ``.rstrip('/')`` antes de concatenar ``/v1/dispatch``. O ``endpoint_url``
    novo deve seguir a mesma normalização para evitar ``//v1/dispatch``.
    """
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "http://envvar-worker:8766")

    captured: list[str] = []
    cli = _install_capture_transport(monkeypatch, captured)
    await cli.dispatch(
        _good_payload(), wait=False,
        endpoint_url="http://claude-worker:8767/",  # trailing slash
    )

    assert captured
    url = captured[0]
    assert "//v1/dispatch" not in url, (
        f"double slash in path indicates trailing-slash not normalized: {url}"
    )
    assert "/v1/dispatch" in url
    assert "claude-worker:8767" in url


async def test_dispatch_endpoint_url_empty_string_falls_back_to_env(monkeypatch):
    """``endpoint_url=""`` (falsy) cai no env var — mesma semântica de ``None``.

    Defesa contra o caller que passa string vazia por engano (ex:
    ``os.environ.get(...)`` retornando string vazia). Mantém alinhado com o
    helper ``_resolve_endpoint`` que considera empty == ausente.
    """
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "http://envvar-worker:8766")

    captured: list[str] = []
    cli = _install_capture_transport(monkeypatch, captured)
    await cli.dispatch(_good_payload(), wait=False, endpoint_url="")

    assert captured
    assert any("envvar-worker:8766" in url for url in captured), (
        f"empty endpoint_url should fall back to env: {captured}"
    )
