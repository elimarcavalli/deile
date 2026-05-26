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

from pydantic import (BaseModel, Field, ValidationError, ValidationInfo,
                      field_validator)

from deile.core.exceptions import DEILEError

logger = logging.getLogger(__name__)

#: Default per-task timeout (seconds). Aligned with ``DEILE_WORKER_TASK_TIMEOUT_S``
#: on the worker side so the client never gives up BEFORE the server has had a
#: chance to finish (regression observed on PR #293: server raised to 900s, but
#: client was stuck at 600s+60s buffer = 660s — review timed out client-side
#: while the worker was still working).
DEFAULT_TIMEOUT_S: float = float(os.environ.get("DEILE_WORKER_TASK_TIMEOUT_S", "600"))
# Budget máximo permitido para um dispatch ``wait=True`` — compartilhado
# entre o ``max_execution_time`` da tool e o timeout do cliente httpx, de
# modo que um cancel upstream não mascare ``WORKER_TIMEOUT`` como
# ``CancelledError``. The +60s buffer absorbs network/serialization latency
# beyond the server's wall-clock budget.
MAX_DISPATCH_BUDGET_S: float = DEFAULT_TIMEOUT_S + 60.0
_NOWAIT_TIMEOUT_S: float = 30.0

_DISPATCH_PATH = "/v1/dispatch"
_PROGRESS_PATH = "/v1/progress/{task_id}"
_RESULT_PATH = "/v1/result/{task_id}"
_POLL_TIMEOUT_S: float = 5.0
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
# header injection em caso de secret file corrompido. O floor de 16
# caracteres alinha com ``secrets_scanner`` (``DEILE_BOT_AUTH_TOKEN`` /
# ``DEILE_WORKER_BEARER_TOKEN`` exigem ``{16,}`` no scanner — ver pilar
# 08 §"Padrões cobertos"); manter o floor uniforme garante que o scanner
# e o validador concordem sobre o que é "token plausível".
_TOKEN_SAFE_CHARS = re.compile(r"^[A-Za-z0-9._\-+/=:~]{16,4096}$")

# Personas suportadas pelo worker — espelha
# deile/personas/library/*.yaml. Manter sincronizado quando uma persona
# nova for adicionada ao worker. ``reviewer`` é a persona do quality-gate
# de review de PR (pilar 04 §Personas; instruções em
# personas/instructions/reviewer.md), usada pelo estágio de review do pipeline.
WorkerPersona = Literal["developer", "architect", "debugger", "reviewer", "analyst"]

# Per-stage model override slug (issue #305): ``provider:model`` (e.g.
# ``deepseek:deepseek-v4-pro``, ``anthropic:claude-sonnet-4-6``). Mirrors
# ``_MODEL_SLUG_RE`` in ``deile/config/settings.py`` — keep in sync.
_MODEL_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*:[a-z0-9._-]+$")


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
    # Recent channel history rendered by the bot's ingress pipeline
    # (``render_history_for_worker``) on the bot-mediated path so the worker
    # can resolve follow-ups. Absent on the ``/deile`` passthrough, keeping
    # that path one-shot. Generous cap: the renderer already bounds it to
    # ~8000 chars and the worker re-truncates, so this only guards against a
    # pathological payload — it must not hard-reject a legitimate render.
    history: Optional[str] = Field(default=None, max_length=20000)
    # Per-turn model override (issue #305) — when set, the worker injects it
    # into ``session.context_data["preferred_model"]`` so the agent's
    # ``_choose_provider_for_turn`` picks the model for THIS dispatch only.
    # The pipeline uses it to give each stage (classify/refine/implement/
    # pr_review/follow_ups) a different model; tools/CLI callers leave it None.
    preferred_model: Optional[str] = Field(default=None, max_length=128)
    # --- Pipeline context (issue #309 fase 2) -------------------------------
    # Todos opcionais. O worker (deile-worker ou claude-worker) usa quando
    # presente, e ignora silenciosamente quando ausente — workers antigos que
    # não conhecem estes campos continuam funcionando porque o cliente serializa
    # com ``model_dump(exclude_none=True)``, omitindo os campos None do wire.
    # ``stage``: qual etapa do pipeline está despachando (mapeia 1-pra-1 com
    # :data:`deile.orchestration.pipeline.dispatch_resolver.PIPELINE_STAGES`).
    # Validado contra esse tuple — typo aqui só apareceria como 5xx do worker
    # muito mais tarde, então falhar local é melhor.
    stage: Optional[str] = Field(default=None, max_length=32)
    # ``action_kind``: tipo de ação concreto (``implement|review|mention|
    # refine|decompose|...``). Não validamos contra um enum aqui porque o
    # conjunto evolui mais rápido que o stage tuple; o worker é a autoridade.
    action_kind: Optional[str] = Field(default=None, max_length=32)
    # ``issue_number``: número da issue GitHub que originou o dispatch (quando
    # houver). Permite o worker fazer telemetry/log correlation.
    issue_number: Optional[int] = Field(default=None, ge=1)
    # ``branch``: nome da branch git de trabalho (ex.: ``auto/issue-309``).
    # Útil pro worker resolver o working tree correto em modos future.
    branch: Optional[str] = Field(default=None, max_length=255)

    @field_validator("brief")
    @classmethod
    def _strip_brief(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("brief must not be blank")
        return stripped

    @field_validator("preferred_model")
    @classmethod
    def _validate_model_slug(cls, v: Optional[str]) -> Optional[str]:
        """Reject malformed slugs at the wire boundary (issue #305).

        ``None`` / empty / whitespace collapse to ``None`` (no override).
        A typo at this layer would only surface as a 5xx many minutes later
        on the worker side, so we fail fast with a precise message.
        """
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        if not _MODEL_SLUG_RE.match(stripped):
            raise ValueError(
                f"preferred_model must match 'provider:model' (got {stripped!r})"
            )
        return stripped

    @field_validator("stage")
    @classmethod
    def _validate_stage(cls, v: Optional[str]) -> Optional[str]:
        """Reject unknown stages at the wire boundary (issue #309 fase 2).

        Importado lazy pra evitar ciclo de import (este módulo de
        infraestrutura é importado cedo no bootstrap, antes do pacote
        :mod:`deile.orchestration.pipeline`). ``None``/empty/whitespace
        colapsam pra ``None`` (sem override de contexto de pipeline).
        """
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
        # Lazy import — quebra ciclo potencial.
        from deile.orchestration.pipeline.dispatch_resolver import \
            PIPELINE_STAGES
        if stripped not in PIPELINE_STAGES:
            raise ValueError(
                f"invalid stage {stripped!r}; expected one of {PIPELINE_STAGES}"
            )
        return stripped

    @field_validator("channel_id", "user_message_id")
    @classmethod
    def _strip_optional_str(
        cls, v: Optional[str], info: ValidationInfo
    ) -> Optional[str]:
        # Pydantic v2 ``@field_validator`` defaults to ``mode='after'``, so
        # the ``min_length=1`` constraint already ran on the raw value
        # before this validator. After stripping, an empty result must be
        # rejected explicitly here — returning ``""`` would silently pass.
        # On the optional ``user_message_id`` we collapse to ``None`` so
        # ``model_dump(exclude_none=True)`` drops the field on the wire.
        if v is None:
            return v
        stripped = v.strip()
        if stripped:
            return stripped
        if info.field_name == "user_message_id":
            return None
        raise ValueError(f"{info.field_name} must not be whitespace-only")


def validate_dispatch_payload(payload: Dict[str, Any]) -> DispatchPayload:
    """Validate a raw dispatch ``payload`` dict against the wire contract.

    Single source of payload validation (pilar 03 §2 — the wire contract is
    owned by this adapter). Raises :class:`WorkerDispatchError` with
    ``error_code="BAD_REQUEST"`` on any schema violation, so the bot-side tool
    (which validates pre-cooldown so a rejection never consumes the channel
    slot) and :meth:`DeileWorkerClient.dispatch` (the authoritative pre-wire
    gate for callers that bypass the tool) reject malformed payloads
    identically instead of each rebuilding the error inline.
    """
    try:
        return DispatchPayload.model_validate(payload)
    except ValidationError as exc:
        # Build the rejection from loc+msg only — never echo input values.
        # ``brief``/``channel_id`` carry untrusted (Discord) content that may
        # include PII, and this message is surfaced to the LLM and logged
        # (pilar 08 — never log request bodies/secrets).
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<payload>'}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)
        )
        raise WorkerDispatchError(
            f"invalid dispatch payload: {details[:200]}",
            error_code="BAD_REQUEST",
        ) from exc
    except Exception as exc:
        raise WorkerDispatchError(
            f"invalid dispatch payload: {type(exc).__name__}",
            error_code="BAD_REQUEST",
        ) from exc


def build_dispatch_payload(
    *,
    brief: str,
    channel_id: str,
    persona: str = "developer",
    wait: bool = True,
    user_message_id: Optional[Any] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    history: Optional[str] = None,
    preferred_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the JSON body POSTed to ``POST /v1/dispatch``.

    Wire-format builder — falsy ``user_message_id`` / ``attachments`` /
    ``preferred_model`` are dropped so ``model_dump(exclude_none=True)`` keeps
    the payload minimal. Lives in the infrastructure module because the wire
    format is owned by the worker adapter, not by the bot-side tool.

    ``preferred_model`` (issue #305) is the per-turn model override the
    pipeline uses to dispatch each stage to a different LLM; tool / CLI
    callers leave it ``None`` and the worker resolves the model from its own
    ``DEILE_PREFERRED_MODEL`` / ``settings.preferred_model``.
    """
    payload: Dict[str, Any] = {
        "brief": brief,
        "channel_id": channel_id,
        "persona": persona,
        "wait_for_result": wait,
    }
    if user_message_id:
        payload["user_message_id"] = str(user_message_id)
    if attachments:
        payload["attachments"] = attachments
    if history:
        payload["history"] = str(history)
    if preferred_model:
        payload["preferred_model"] = str(preferred_model)
    return payload


def summarize_dispatch_response(data: Any) -> str:
    """Compact one-line summary of a worker dispatch response for the bot LLM.

    The user already sees the rich status message edited live by the worker,
    so this stays terse on purpose — do NOT echo the full output. Lives in
    the infrastructure module because the response shape (``ok``, ``files``,
    ``elapsed_s``, ``task_id``) is owned by the worker adapter.
    """
    if not isinstance(data, dict):
        return ""
    ok = data.get("ok")
    if ok is True:
        files = data.get("files")
        if not isinstance(files, list):
            files = []
        try:
            elapsed = float(data.get("elapsed_s") or 0)
        except (TypeError, ValueError):
            elapsed = 0.0
        return (
            f"worker concluiu em {elapsed:.1f}s — "
            f"{len(files)} arquivo(s): " + ", ".join(str(f) for f in files[:5])
        )
    if ok is False:
        return (
            f"worker FALHOU: {str(data.get('summary') or data.get('error'))[:300]}"
        )
    return (
        f"worker dispatch aceito (task_id={data.get('task_id')}); "
        "use wait_for_result=true para acompanhar."
    )


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

    Resolução por **precedência** (primeiro match não-vazio vence — não é
    fallback; uma fonte anterior não-vazia esconde todas as seguintes):

      1. env var ``DEILE_WORKER_BEARER_TOKEN`` (set pelo wrapper antes do bootstrap)
      2. arquivo ``/run/secrets/bot/worker/AUTH_TOKEN`` (bot pod, mount real K8s)
      3. arquivo ``/run/secrets/worker/AUTH_TOKEN``     (worker pod)
      4. arquivo ``/run/secrets/bot/WORKER_BEARER_TOKEN`` (fallback legado)

    O código-fonte resolvido é emitido em ``logger.debug`` (sem o valor)
    para permitir diagnóstico via ``kubectl logs | grep "token resolved"``.

    Faz I/O de disco bloqueante — deve ser chamada via ``asyncio.to_thread``.
    Mesma razão de ``_resolve_endpoint``: leitura pré-bootstrap, sem
    ``Settings``.
    """
    val = os.environ.get(_TOKEN_ENV, "").strip()
    if val:
        logger.debug("worker token resolved from env (%s)", _TOKEN_ENV)
        return val
    for path in _TOKEN_FILES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                v = f.read().strip()
        except OSError:
            continue
        if v:
            logger.debug("worker token resolved from %s", path)
            return v
    return ""


def _validate_token_charset(token: str) -> bool:
    """True se ``token`` não tem CR/LF/NUL e cabe no charset bearer comum."""
    return bool(_TOKEN_SAFE_CHARS.match(token))


async def _resolve_auth_and_httpx() -> tuple[str, Any]:
    """Shared bootstrap: resolve token (off-loop), validate it, import httpx.

    Returns ``(token, httpx_module)``. Raises :class:`WorkerDispatchError`
    with the same ``error_code`` codes that ``dispatch`` / ``_get_json``
    previously emitted inline (``WORKER_AUTH_MISSING``,
    ``WORKER_AUTH_MALFORMED``, ``WORKER_TRANSPORT_MISSING``) — kept in one
    place so any future change (e.g. cached token, alternate transport)
    lands in a single helper.
    """
    token = await asyncio.to_thread(_read_token)
    if not token:
        raise WorkerDispatchError(
            "WORKER_BEARER_TOKEN not configured in this Pod",
            error_code="WORKER_AUTH_MISSING",
        )
    if not _validate_token_charset(token):
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
    return token, httpx


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
        #
        # Defense-in-depth: this is the LAST line of validation before the
        # wire. ``DispatchDeileTaskTool.execute()`` also calls
        # ``validate_dispatch_payload(payload)`` before recording the
        # cooldown, but callers that bypass the tool (custom scripts, tests
        # constructing payloads by hand) must NOT skip-validate here —
        # ``DispatchPayload`` is the contract with the worker and the
        # validation belongs to this adapter as the authoritative gatekeeper.
        validated = validate_dispatch_payload(payload)
        body = validated.model_dump(exclude_none=True)

        endpoint = _resolve_endpoint().rstrip("/") + _DISPATCH_PATH
        # Token resolution touches secret files on disk — keep that blocking
        # I/O off the event loop. The token is a secret: it must never be
        # interpolated into log or error messages.
        token, httpx = await _resolve_auth_and_httpx()

        request_id = str(uuid.uuid4())
        timeout: float = MAX_DISPATCH_BUDGET_S if wait else _NOWAIT_TIMEOUT_S
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
            err = (data.get("error") if isinstance(data, dict) else None) or {}
            code = err.get("code") or "WORKER_ERROR"
            msg = err.get("message") or f"HTTP {resp.status_code}"
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

    async def get_progress(self, task_id: str) -> Dict[str, Any]:
        """``GET /v1/progress/{task_id}`` — snapshot mid-flight.

        Usado pelo :class:`WorkerSubAgentRunner` (issue #257) para polling.
        Retorna o snapshot do ``_TASKS[task_id]`` no worker, incluindo
        ``progress_lines``, ``phase``, ``current_activity`` e ``ok`` (None
        enquanto em execução).
        """
        return await self._get_json(_PROGRESS_PATH.format(task_id=task_id))

    async def get_result(self, task_id: str) -> Dict[str, Any]:
        """``GET /v1/result/{task_id}`` — resultado final (após término).

        Distinto de :meth:`get_progress` em intenção: ``get_result`` é
        chamado uma única vez após detectar o terminal via progress
        polling, para capturar ``files`` e ``summary`` definitivos.
        """
        return await self._get_json(_RESULT_PATH.format(task_id=task_id))

    async def _get_json(self, path: str) -> Dict[str, Any]:
        """Helper compartilhado: GET autenticado + JSON parsing.

        Reusa a resolução de endpoint/token de :meth:`dispatch`, mas com
        timeout curto (polling, não dispatch). Erros mapeiam para os mesmos
        :class:`WorkerDispatchError` codes; o caller decide se é fatal.
        """
        endpoint = _resolve_endpoint().rstrip("/") + path
        token, httpx = await _resolve_auth_and_httpx()

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=_POLL_TIMEOUT_S) as cli:
            try:
                resp = await cli.get(endpoint, headers=headers)
            except httpx.TimeoutException as exc:
                raise WorkerDispatchError(
                    f"worker poll timeout: {str(exc)[:200]}",
                    error_code="WORKER_TIMEOUT",
                ) from exc
            except httpx.HTTPError as exc:
                raise WorkerDispatchError(
                    f"worker unreachable: {type(exc).__name__}: {str(exc)[:200]}",
                    error_code="WORKER_UNREACHABLE",
                ) from exc

        # M9 (PR #295 review): checa status_code ANTES de tentar parsear JSON.
        # 404 é transiente logo após o dispatch (a task pode ainda não estar
        # registrada no _TASKS); retornar NOT_FOUND tipado deixa o caller
        # (WorkerSubAgentRunner) retentar especificamente esse caso. Erros
        # ≥500 viram SERVER_ERROR sem depender de o body ser JSON-parseable
        # (o nginx/proxy pode devolver HTML 502).
        #
        # Iter-2 review: discrimina 401/403/5xx ANTES do json parse — um
        # auth proxy que devolve HTML em 401 antes mapeava para
        # WORKER_BAD_RESPONSE e o caller (loop de retry) tratava como
        # transient. Agora cada classe de status vira um error_code
        # específico para que o caller possa decidir corretamente.
        if resp.status_code == 404:
            raise WorkerDispatchError(
                f"task not found at {path}",
                error_code="NOT_FOUND",
            )
        if resp.status_code in (401, 403):
            raise WorkerDispatchError(
                f"worker auth/forbidden (status={resp.status_code}) at {path}",
                error_code="WORKER_AUTH_ERROR" if resp.status_code == 401
                else "WORKER_FORBIDDEN",
            )
        if resp.status_code >= 500:
            raise WorkerDispatchError(
                f"worker server error (status={resp.status_code}) at {path}",
                error_code="WORKER_SERVER_ERROR",
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            # Status já filtrou 401/403/5xx acima; o que sobra são 2xx e
            # 4xx outros. 4xx sem JSON parseable → BAD_REQUEST. 2xx →
            # body inválido, ainda BAD_RESPONSE (era o original).
            if resp.status_code >= 400:
                raise WorkerDispatchError(
                    f"worker bad request (status={resp.status_code}, non-JSON body)",
                    error_code="WORKER_BAD_REQUEST",
                ) from exc
            raise WorkerDispatchError(
                f"worker returned non-JSON (status={resp.status_code})",
                error_code="WORKER_BAD_RESPONSE",
            ) from exc

        if resp.status_code >= 400:
            err = (data.get("error") if isinstance(data, dict) else None) or {}
            code = err.get("code") or "WORKER_BAD_REQUEST"
            msg = err.get("message") or f"HTTP {resp.status_code}"
            raise WorkerDispatchError(msg, error_code=code)
        if not isinstance(data, dict):
            raise WorkerDispatchError(
                "worker returned non-object body",
                error_code="WORKER_BAD_RESPONSE",
            )
        return data
