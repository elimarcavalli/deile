"""Adapter de transporte para o control plane do deile-monitor.

Cópia endurecida de :mod:`deile.infrastructure.deile_worker_client`
(arquitetura hexagonal — pilar 03 §2): isola a fronteira de
infraestrutura do proxy do bot. Resolve endpoint e credenciais — lendo
variáveis de ambiente e arquivos de secret montados pelo K8s — e faz o
transporte HTTP via ``httpx``. O código de domínio (o proxy do bot)
consome apenas :class:`MonitorClient`, sem tocar em ``os.environ``, no
filesystem de secrets nem no SDK HTTP diretamente.

O servidor que este cliente consome é ``infra/k8s/monitor_command_server.py``
(``:8769``). As rotas e os shapes de resposta espelhados aqui:

* ``GET  /v1/monitor-status`` → status determinístico (dict)
* ``POST /v1/command``        → ``{"accepted", "command", "effect"}`` (400 → envelope ``error``)
* ``POST /v1/ask``            → 202 ``{"request_id", "status"}``
* ``GET  /v1/ask/{id}``       → job dict (404 → ``NOT_FOUND``)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

from deile.common.bearer import _TOKEN_SAFE_CHARS, _validate_token_charset
from deile.core.exceptions import DEILEError

logger = logging.getLogger(__name__)

_DEFAULT_ENDPOINT = "http://deile-monitor:8769"
_ENDPOINT_ENV = "DEILE_MONITOR_ENDPOINT"
_TOKEN_ENV = "DEILE_MONITOR_AUTH_TOKEN"
# Secret files, em ordem de precedência. Tolera o layout do pod do bot
# (``/run/secrets/bot/monitor/...``) e o layout do próprio monitor
# (``/run/secrets/monitor/...``).
_TOKEN_FILES = (
    "/run/secrets/bot/monitor/MONITOR_BEARER_TOKEN",
    "/run/secrets/monitor/MONITOR_BEARER_TOKEN",
)

_STATUS_PATH = "/v1/monitor-status"
_COMMAND_PATH = "/v1/command"
_ASK_PATH = "/v1/ask"
_ASK_RESULT_PATH = "/v1/ask/{request_id}"

# Timeout estruturado: o monitor-status/command/ask retornam rápido (o ``ask``
# é 202 fire-and-forget, o resultado é colhido por polling em ``get_ask_result``),
# então um budget total curto basta. Mantemos um teto separado para connect/pool
# de modo que uma falha de rede falhe rápido em vez de esperar o budget inteiro.
_TOTAL_TIMEOUT_S: float = 30.0
_CONNECT_TIMEOUT_S: float = 30.0
_POOL_TIMEOUT_S: float = 30.0


class MonitorClientError(DEILEError):
    """Falha ao falar com o deile-monitor.

    Carrega um ``code`` (mapeado para ``error_code`` da base
    :class:`DEILEError`) que o proxy do bot repassa ao usuário. A assinatura
    é ``(code, message)`` — posicional, código primeiro — para deixar o
    call-site do mapeamento de erros conciso.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message, error_code=code)
        self.code = code


# Módulo-level (não @staticmethod) propositalmente — facilita monkeypatching
# nos testes sem ter que instanciar o cliente.
def _resolve_endpoint() -> str:
    """Resolve a base URL do monitor, sem barra final.

    Lemos de ``os.environ`` diretamente (em vez do ``Settings`` singleton)
    porque o endpoint é resolvido em runtime ANTES do bootstrap completo —
    mesma razão do deile_worker_client.
    """
    return os.environ.get(_ENDPOINT_ENV, _DEFAULT_ENDPOINT).rstrip("/")


def _read_token() -> str:
    """Resolve o bearer token por precedência (primeiro match não-vazio vence):

      1. env var ``DEILE_MONITOR_AUTH_TOKEN``
      2. arquivo ``/run/secrets/bot/monitor/MONITOR_BEARER_TOKEN`` (pod do bot)
      3. arquivo ``/run/secrets/monitor/MONITOR_BEARER_TOKEN``     (pod do monitor)

    Faz I/O de disco bloqueante — deve ser chamada via ``asyncio.to_thread``.
    O valor nunca é emitido em log (apenas a fonte resolvida, em ``debug``).
    """
    val = os.environ.get(_TOKEN_ENV, "").strip()
    if val:
        logger.debug("monitor token resolved from env (%s)", _TOKEN_ENV)
        return val
    for path in _TOKEN_FILES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
        except OSError:
            continue
        if v:
            logger.debug("monitor token resolved from %s", path)
            return v
    return ""


async def _resolve_auth_and_httpx() -> tuple[str, Any]:
    """Bootstrap compartilhado: resolve o token (off-loop), valida-o, importa httpx.

    Retorna ``(token, httpx_module)``. Levanta :class:`MonitorClientError` com
    os códigos ``MONITOR_AUTH_MISSING`` / ``MONITOR_AUTH_MALFORMED`` /
    ``MONITOR_TRANSPORT_MISSING`` — em um único lugar, espelhando o
    deile_worker_client.
    """
    token = await asyncio.to_thread(_read_token)
    if not token:
        raise MonitorClientError(
            "MONITOR_AUTH_MISSING",
            "MONITOR_BEARER_TOKEN not configured in this Pod",
        )
    if not _validate_token_charset(token):
        raise MonitorClientError(
            "MONITOR_AUTH_MALFORMED",
            "bearer token has invalid characters",
        )
    try:
        import httpx
    except ImportError as exc:
        raise MonitorClientError(
            "MONITOR_TRANSPORT_MISSING",
            "httpx is not installed in this image",
        ) from exc
    return token, httpx


class MonitorClient:
    """Cliente do control plane do deile-monitor (``:8769``).

    Stateless: endpoint e token são resolvidos a cada chamada, refletindo
    qualquer mudança de ambiente sem reinstanciar o cliente. O
    ``httpx.AsyncClient`` é instanciado por chamada — chamadas são raras e
    instâncias longevas mascarariam mudanças de token/endpoint.
    """

    async def get_status(self) -> Dict[str, Any]:
        """``GET /v1/monitor-status`` — estado determinístico (sem LLM)."""
        return await self._request("GET", _STATUS_PATH)

    async def post_command(self, command: str) -> Dict[str, Any]:
        """``POST /v1/command`` — ordens pause/resume/ack/force-tick.

        Em HTTP 400 o servidor devolve ``{"error": {"code", "message"}}``
        (ex.: ``BAD_COMMAND``); desempacotamos e levantamos
        :class:`MonitorClientError` com o código do servidor.
        """
        return await self._request(
            "POST", _COMMAND_PATH, json_body={"command": command}
        )

    async def ask(self, question: str) -> str:
        """``POST /v1/ask`` — Q&A free-form. Espera 202 e retorna o ``request_id``.

        O servidor enfileira a pergunta e responde 202 imediatamente; a
        resposta é colhida depois via :meth:`get_ask_result`.
        """
        data = await self._request(
            "POST", _ASK_PATH, json_body={"question": question}
        )
        request_id = data.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise MonitorClientError(
                "MONITOR_BAD_RESPONSE",
                "ask response missing request_id",
            )
        return request_id

    async def get_ask_result(self, request_id: str) -> Dict[str, Any]:
        """``GET /v1/ask/{request_id}`` — poll do resultado do Q&A.

        404 (request_id desconhecido) → :class:`MonitorClientError`
        ``MONITOR_NOT_FOUND``.
        """
        return await self._request(
            "GET", _ASK_RESULT_PATH.format(request_id=request_id)
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """GET/POST autenticado + parsing de JSON, com mapeamento de erros.

        Mapeamento (espelha o deile_worker_client):

        * ``httpx.TimeoutException`` → ``MONITOR_TIMEOUT``
        * ``httpx.HTTPError``        → ``MONITOR_UNREACHABLE``
        * body não-JSON              → ``MONITOR_BAD_RESPONSE``
        * HTTP 401                   → ``MONITOR_AUTH_ERROR``
        * HTTP 404                   → ``MONITOR_NOT_FOUND``
        * HTTP ≥ 400 com envelope    → ``code`` do envelope
        * HTTP ≥ 400 sem envelope    → ``MONITOR_HTTP_ERROR``

        Nunca deixa uma exceção crua escapar — sempre levanta
        :class:`MonitorClientError`. ``asyncio.CancelledError`` é re-raised
        intacto.
        """
        endpoint = _resolve_endpoint() + path
        token, httpx = await _resolve_auth_and_httpx()

        request_id = str(uuid.uuid4())
        timeout = httpx.Timeout(
            _TOTAL_TIMEOUT_S,
            connect=min(_CONNECT_TIMEOUT_S, _TOTAL_TIMEOUT_S),
            pool=min(_POOL_TIMEOUT_S, _TOTAL_TIMEOUT_S),
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "X-Request-ID": request_id,
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        async with httpx.AsyncClient(timeout=timeout) as cli:
            try:
                resp = await cli.request(
                    method, endpoint, json=json_body, headers=headers
                )
            except asyncio.CancelledError:
                raise
            except httpx.TimeoutException as exc:
                raise MonitorClientError(
                    "MONITOR_TIMEOUT",
                    f"monitor timeout after {_TOTAL_TIMEOUT_S}s "
                    f"(request_id={request_id}): {str(exc)[:200]}",
                ) from exc
            except httpx.HTTPError as exc:
                raise MonitorClientError(
                    "MONITOR_UNREACHABLE",
                    f"monitor unreachable: {type(exc).__name__} "
                    f"(request_id={request_id}): {str(exc)[:200]}",
                ) from exc

        # Discrimina os status que têm código próprio ANTES do parse de JSON
        # (um proxy de auth pode devolver HTML em 401), e só então tenta o body.
        if resp.status_code == 401:
            raise MonitorClientError(
                "MONITOR_AUTH_ERROR",
                f"monitor auth error (status=401) at {path}",
            )
        if resp.status_code == 404:
            raise MonitorClientError(
                "MONITOR_NOT_FOUND",
                f"resource not found at {path}",
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise MonitorClientError(
                "MONITOR_BAD_RESPONSE",
                f"monitor returned non-JSON (status={resp.status_code}, "
                f"request_id={request_id})",
            ) from exc

        if resp.status_code >= 400:
            err_raw = data.get("error") if isinstance(data, dict) else None
            err = err_raw if isinstance(err_raw, dict) else {}
            code = (
                err.get("code")
                or (data.get("error_code") if isinstance(data, dict) else None)
                or "MONITOR_HTTP_ERROR"
            )
            msg = (
                err.get("message")
                or (err_raw if isinstance(err_raw, str) else None)
                or f"HTTP {resp.status_code}"
            )
            raise MonitorClientError(code, f"{msg} (request_id={request_id})")

        if not isinstance(data, dict):
            raise MonitorClientError(
                "MONITOR_BAD_RESPONSE",
                "monitor returned non-object body",
            )
        return data
