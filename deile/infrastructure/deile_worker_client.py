"""Adapter de transporte para o control plane do deile-worker.

Isola a fronteira de infraestrutura do ``dispatch_deile_task`` tool
(arquitetura hexagonal — pilar 03 §2): resolução de endpoint e
credenciais — incluindo leitura de variáveis de ambiente e de arquivos
de secret montados pelo K8s — e o transporte HTTP via ``httpx``. O
código de domínio (a tool) consome apenas :class:`DeileWorkerClient`,
sem tocar em ``os.environ``, no filesystem de secrets nem no SDK HTTP
diretamente.

O modelo Pydantic :class:`DispatchPayload` vive aqui propositalmente:
o contrato de wire-format do worker é da camada de infraestrutura, e
a tool delega a validação a este módulo para não acoplar ao SDK do
Pydantic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from deile.core.exceptions import DEILEError

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S: float = 600.0

_DISPATCH_PATH = "/v1/dispatch"
_DEFAULT_ENDPOINT = "http://deile-worker.deile.svc.cluster.local:8766"
_ENDPOINT_ENV = "DEILE_WORKER_ENDPOINT"
_TOKEN_ENV = "DEILE_WORKER_BEARER_TOKEN"
# Secret files, in resolution order, tolerating both bot and worker layouts.
_TOKEN_FILES = (
    "/run/secrets/bot/worker/AUTH_TOKEN",
    "/run/secrets/worker/AUTH_TOKEN",
    "/run/secrets/bot/WORKER_BEARER_TOKEN",
)

# Tokens são tratados como bearer values: rejeitamos qualquer caractere
# que possa quebrar o header HTTP (CR, LF, NUL) — defense-in-depth contra
# header injection em caso de secret file corrompido.
_TOKEN_SAFE_CHARS = re.compile(r"^[A-Za-z0-9._\-+/=:~]{8,4096}$")

# Personas suportadas pelo worker — espelha
# deile/personas/library/*.yaml. Manter sincronizado quando uma persona
# nova for adicionada ao worker.
WorkerPersona = Literal["developer", "architect", "debugger"]


class WorkerDispatchError(DEILEError):
    """Falha ao despachar uma task para o deile-worker.

    Carrega o ``error_code`` que a tool repassa no ``ToolResult``,
    preservando os códigos de erro originais do ``dispatch_deile_task``.
    """


class DispatchPayload(BaseModel):
    """Wire-format do POST /v1/dispatch.

    Validação Pydantic do payload antes de cruzar a fronteira de rede.
    Rejeita brief vazio, channel_id vazio e personas desconhecidas — o
    worker já valida do lado dele, mas falhar local poupa um round-trip
    e dá uma mensagem de erro melhor ao LLM.
    """

    brief: str = Field(..., min_length=1, max_length=8000)
    channel_id: str = Field(..., min_length=1, max_length=64)
    persona: WorkerPersona = "developer"
    wait_for_result: bool = True
    user_message_id: Optional[str] = Field(default=None, max_length=64)
    attachments: Optional[List[Dict[str, Any]]] = None

    @field_validator("brief")
    @classmethod
    def _strip_brief(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("brief must not be blank")
        return stripped

    @field_validator("channel_id", "user_message_id")
    @classmethod
    def _strip_optional_str(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        stripped = v.strip()
        if not stripped:
            return None if cls.model_fields.get("channel_id") is None else ""
        return stripped


# Módulo-level (não @staticmethod) propositalmente — facilita
# monkeypatching nos testes sem ter que instanciar o cliente.
def _resolve_endpoint() -> str:
    # Lemos de ``os.environ`` diretamente (em vez do ``Settings``
    # singleton) porque ``DEILE_WORKER_ENDPOINT`` é resolvido em runtime
    # ANTES do bootstrap completo do agente — o worker client é
    # instanciado no construtor da tool, que pode rodar antes de
    # ``get_settings()`` estar pronto sob algumas inicializações
    # programáticas.
    return os.environ.get(_ENDPOINT_ENV, _DEFAULT_ENDPOINT)


def _read_token() -> str:
    """Resolve o bearer token. Tolerante a layouts de bot e de worker.

    Ordem de resolução:
      1. env var ``DEILE_WORKER_BEARER_TOKEN`` (set pelo wrapper antes do bootstrap)
      2. arquivo ``/run/secrets/bot/worker/AUTH_TOKEN`` (bot pod, mount real K8s)
      3. arquivo ``/run/secrets/worker/AUTH_TOKEN``     (worker pod)
      4. arquivo ``/run/secrets/bot/WORKER_BEARER_TOKEN`` (fallback legado)

    Faz I/O de disco bloqueante — deve ser chamada via ``asyncio.to_thread``.
    Mesma razão de ``_resolve_endpoint``: leitura pré-bootstrap, sem
    ``Settings``.
    """
    val = os.environ.get(_TOKEN_ENV, "").strip()
    if val:
        return val
    for path in _TOKEN_FILES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return ""


def _validate_token_charset(token: str) -> bool:
    """True se ``token`` não tem CR/LF/NUL e cabe no charset bearer comum."""
    return bool(_TOKEN_SAFE_CHARS.match(token))


class DeileWorkerClient:
    """Cliente do control plane do deile-worker (``POST /v1/dispatch``).

    Stateless: endpoint e token são resolvidos a cada :meth:`dispatch`,
    refletindo qualquer mudança de ambiente sem reinstanciar o cliente.

    O ``httpx.AsyncClient`` é instanciado por chamada (em vez de
    reaproveitado entre dispatches) porque (a) dispatches são raros
    — não há benefício mensurável de keep-alive — e (b) instâncias
    longevas impedem que mudanças no token/endpoint sejam refletidas.
    A escolha custa ~5ms por dispatch, irrelevante frente ao timeout
    de 10min do worker.
    """

    async def dispatch(
        self, payload: Dict[str, Any], *, wait: bool
    ) -> Dict[str, Any]:
        """POST a dispatch payload to the worker and return its parsed JSON body.

        Raises :class:`WorkerDispatchError` — carrying an ``error_code`` — for
        every failure mode: missing/malformed credentials, missing ``httpx``,
        transport timeout/unreachable, non-JSON body, or an HTTP >= 400
        response.
        """
        # Valida o payload antes de tocar em I/O — falhas locais não
        # contam contra o cooldown anti-loop da tool.
        try:
            validated = DispatchPayload.model_validate(payload)
            body = validated.model_dump(exclude_none=True)
        except Exception as exc:
            raise WorkerDispatchError(
                f"invalid dispatch payload: {str(exc)[:200]}",
                error_code="BAD_REQUEST",
            ) from exc

        endpoint = _resolve_endpoint().rstrip("/") + _DISPATCH_PATH
        # Token resolution touches secret files on disk — keep that blocking
        # I/O off the event loop. The token is a secret: it must never be
        # interpolated into log or error messages.
        token = await asyncio.to_thread(_read_token)
        if not token:
            raise WorkerDispatchError(
                "WORKER_BEARER_TOKEN not configured in this Pod",
                error_code="WORKER_AUTH_MISSING",
            )
        if not _validate_token_charset(token):
            # Defense-in-depth: rejeita CR/LF/NUL antes de injetar no
            # header. Não logamos o token nem o seu fingerprint —
            # apenas o code-path.
            raise WorkerDispatchError(
                "bearer token has invalid characters",
                error_code="WORKER_AUTH_MALFORMED",
            )

        try:
            import httpx
        except ImportError as exc:
            raise WorkerDispatchError(
                "httpx is not installed in this image",
                error_code="WORKER_TRANSPORT_MISSING",
            ) from exc

        request_id = str(uuid.uuid4())
        timeout: float = (DEFAULT_TIMEOUT_S + 60.0) if wait else 30.0
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        logger.info(
            "worker dispatch starting",
            extra={"request_id": request_id, "wait": wait},
        )
        async with httpx.AsyncClient(timeout=timeout) as cli:
            try:
                resp = await cli.post(endpoint, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise WorkerDispatchError(
                    f"worker timeout after {timeout}s "
                    f"(request_id={request_id}): {str(exc)[:200]}",
                    error_code="WORKER_TIMEOUT",
                ) from exc
            except httpx.HTTPError as exc:
                raise WorkerDispatchError(
                    f"worker unreachable: {type(exc).__name__} "
                    f"(request_id={request_id}): {str(exc)[:200]}",
                    error_code="WORKER_UNREACHABLE",
                ) from exc

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise WorkerDispatchError(
                f"worker returned non-JSON (status={resp.status_code}, "
                f"request_id={request_id})",
                error_code="WORKER_BAD_RESPONSE",
            ) from exc

        if resp.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else {}
            code = (err or {}).get("code") or "WORKER_ERROR"
            msg = (err or {}).get("message") or f"HTTP {resp.status_code}"
            raise WorkerDispatchError(
                f"{msg} (request_id={request_id})", error_code=code
            )

        logger.info(
            "worker dispatch completed",
            extra={
                "request_id": request_id,
                "status": resp.status_code,
                "task_id": data.get("task_id") if isinstance(data, dict) else None,
            },
        )
        return data
