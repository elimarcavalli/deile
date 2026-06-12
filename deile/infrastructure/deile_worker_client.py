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
import random
import re
import uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import (BaseModel, Field, ValidationError, ValidationInfo,
                      field_validator)

from deile.core.exceptions import DEILEError
from deile.infrastructure.circuit_breaker import CircuitBreaker
from deile.orchestration.pipeline.dispatch_resolver import PIPELINE_STAGES

logger = logging.getLogger(__name__)

#: Default per-task timeout (seconds). Aligned with ``DEILE_WORKER_TASK_TIMEOUT_S``
#: on the worker side so the client never gives up BEFORE the server has had a
#: chance to finish (regression observed on PR #293: server raised to 900s, but
#: client was stuck at 600s+60s buffer = 660s — review timed out client-side
#: while the worker was still working).
DEFAULT_TIMEOUT_S: float = float(os.environ.get("DEILE_WORKER_TASK_TIMEOUT_S", "7200"))
# Budget máximo permitido para um dispatch ``wait=True`` — compartilhado
# entre o ``max_execution_time`` da tool e o timeout do cliente httpx, de
# modo que um cancel upstream não mascare ``WORKER_TIMEOUT`` como
# ``CancelledError``. The +60s buffer absorbs network/serialization latency
# beyond the server's wall-clock budget.
MAX_DISPATCH_BUDGET_S: float = DEFAULT_TIMEOUT_S + 60.0
_NOWAIT_TIMEOUT_S: float = 30.0

# Connect/pool ceilings independentes do budget total (regressão observada em
# produção 2026-06-01: o ``deile-pipeline`` congelou ~52min após "worker
# dispatch starting" porque ``httpx.AsyncClient(timeout=<float>)`` aplica o
# MESMO valor a connect/read/write/pool — com ``MAX_DISPATCH_BUDGET_S`` ≈ 2h,
# um socket pendurado na fase de connect/handshake só estouraria depois de 2h,
# travando o tick inteiro). Mantemos ``read``/``write`` no budget do task
# (o worker pode legitimamente levar até 2h), mas ``connect`` e ``pool`` ganham
# um teto curto para que uma falha de rede falhe rápido. O hard-stop final
# (mesmo que ``read`` legitimamente seja 2h) é garantido pelo
# ``asyncio.wait_for`` no nível do tick (``WorkerImplementer._dispatch``).
_CONNECT_TIMEOUT_S: float = 30.0
_POOL_TIMEOUT_S: float = 30.0

# Retry com exponential backoff (issue #620 AC4). Só re-tentamos falhas
# TRANSIENTES (timeout/inalcançável/5xx) — 4xx (incl. 409/429) refletem o
# pedido em si e nunca são re-tentados. ``_RETRY_MAX_ATTEMPTS`` é o teto
# absoluto; ``DispatchPayload.max_retries`` (quando presente) o reduz ainda
# mais. Backoff entre as tentativas k=0,1,...: ``base * factor^k ± jitter``.
_RETRY_MAX_ATTEMPTS: int = 3
_RETRY_BASE_S: float = 1.0
_RETRY_FACTOR: float = 2.0
_RETRY_JITTER_S: float = 0.3
#: error_codes que indicam falha transitória e portanto re-tentável.
_RETRYABLE_ERROR_CODES: frozenset = frozenset(
    {"WORKER_TIMEOUT", "WORKER_UNREACHABLE", "WORKER_SERVER_ERROR"}
)

# Circuit breaker compartilhado pelo processo (issue #620 AC5): threshold de
# 5 falhas consecutivas abre o circuito; reset de 30s leva ao probe half-open.
# Instância única por processo — o estado de saúde do worker é global, não
# por-dispatch. Exposto via ``circuit_breaker_state()`` para a métrica gauge.
_CIRCUIT_FAILURE_THRESHOLD: int = 5
_CIRCUIT_RESET_TIMEOUT_S: float = 30.0
_CIRCUIT_BREAKER = CircuitBreaker(
    failure_threshold=_CIRCUIT_FAILURE_THRESHOLD,
    reset_timeout_s=_CIRCUIT_RESET_TIMEOUT_S,
)


def circuit_breaker_state() -> int:
    """Estado corrente do circuit breaker como inteiro (0=closed, 1=open,
    2=half-open) — espelha o gauge ``deile_worker_circuit_breaker_state``."""
    return int(_CIRCUIT_BREAKER.state)


def reset_circuit_breaker() -> None:
    """Reseta o circuit breaker do processo para CLOSED.

    Destinado a testes (o breaker é um singleton de processo, então estado
    de um teste vazaria para o seguinte) e a bootstrap. Nunca chamado no
    caminho de dispatch.
    """
    _CIRCUIT_BREAKER.reset()

_DISPATCH_PATH = "/v1/dispatch"
_PROGRESS_PATH = "/v1/progress/{task_id}"
_RESULT_PATH = "/v1/result/{task_id}"
_RESUME_INFO_PATH = "/v1/dispatches/{task_id}/resume-info"
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
# ``deepseek:deepseek-v4-pro``, ``anthropic:claude-sonnet-4-6``,
# ``openrouter:anthropic/claude-sonnet-4.6``). Mirrors ``_MODEL_SLUG_RE`` in
# ``deile/config/settings.py`` — keep in sync. The ``/`` is required for the
# OpenRouter gateway, whose model ids embed the upstream vendor.
_MODEL_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*:[a-z0-9._/-]+$")


class WorkerDispatchError(DEILEError):
    """Falha ao despachar uma task para o deile-worker.

    Carrega o ``error_code`` que a tool repassa no ``ToolResult``,
    preservando os códigos de erro originais do ``dispatch_deile_task``.

    ``http_status`` (quando a falha veio de uma resposta HTTP) carrega o
    status code para a lógica de retry classificar 5xx (transitório,
    re-tentável) vs 4xx (definitivo, sem retry) — issue #620 AC4.
    """

    def __init__(self, *args, http_status: Optional[int] = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.http_status = http_status

    def is_retryable(self) -> bool:
        """True se a falha é transitória: timeout, inalcançável ou HTTP 5xx.

        4xx (incl. 409 duplicate, 429 rate-limit) NUNCA é re-tentável — o
        erro está no pedido/estado, não no transporte (issue #620 AC4).
        """
        if self.http_status is not None:
            return self.http_status >= 500
        return self.error_code in _RETRYABLE_ERROR_CODES


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
    # CLI-worker model override (frota multi-CLI). Diferente de
    # ``preferred_model``, este é o **model-id NATIVO do CLI** — string LIVRE
    # (ex.: ``openrouter/deepseek/deepseek-chat``, ``qwen3-coder-plus``), sem o
    # regex ``provider:model`` do deile-worker (relaxar aquele validator
    # quebraria a fronteira de wire do deile-worker; por isso é campo separado).
    # Só é populado quando o stage roteia para um worker CLI (``*-worker``); o
    # deile-worker/claude-worker IGNORAM este campo (consomem ``preferred_model``).
    # Resolvido por ``model_resolver.resolve_stage_cli_model``.
    cli_model: Optional[str] = Field(default=None, max_length=256)
    # Per-turn reasoning-effort override. Resolvido por
    # ``reasoning_resolver.resolve_stage_reasoning``; o deile-worker injeta em
    # ``session.context_data["reasoning_effort"]`` (provider traduz para o
    # parâmetro nativo) e o claude-worker o passa a ``claude --effort``.
    # Vocabulário em ``deile/core/models/reasoning.py``.
    preferred_reasoning: Optional[str] = Field(default=None, max_length=32)
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
    # --- Resume context (issue #309 fase 3.5) -------------------------------
    # ``resume_session_id``: UUID da sessão claude do dispatch anterior. Quando
    # presente, o claude-worker spawna ``claude -p -r <session_id>`` em vez
    # de ``--session-id <new_uuid>`` — claude lê o JSONL persistido e retoma a
    # conversa em vez de começar do zero. Sem esse campo, dispatch é fresh.
    resume_session_id: Optional[str] = Field(default=None, max_length=64)
    # ``prev_task_id``: hex 16-char do dispatch anterior. claude-worker valida
    # contra session.json persistido (workdir, session_id bate) antes de
    # retomar — devolve 404/410/409 com error_code específico se invalido,
    # pra o pipeline fallback pra fresh dispatch.
    prev_task_id: Optional[str] = Field(default=None, max_length=16)
    # --- Per-stage dispatch tuning (issue #391) ---------------------------------
    # ``timeout_s``: wall-clock seconds before the worker kills the subprocess /
    # marks the task as timed-out. When set, overrides the worker's own default
    # ``DEILE_WORKER_TASK_TIMEOUT_S`` for THIS dispatch only. Resolved by
    # :func:`deile.orchestration.pipeline.dispatch_resolver.resolve_stage_timeout_s`
    # before the payload is assembled.
    timeout_s: Optional[int] = Field(default=None, ge=1)
    # ``max_retries``: maximum number of attempts before the pipeline escalates
    # to ``~workflow:bloqueada``. 0 = no retry (fail immediately). Resolved by
    # :func:`deile.orchestration.pipeline.dispatch_resolver.resolve_stage_max_retries`.
    max_retries: Optional[int] = Field(default=None, ge=0)

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

    @field_validator("cli_model")
    @classmethod
    def _validate_cli_model(cls, v: Optional[str]) -> Optional[str]:
        """Strip e colapsa vazio para ``None`` — string LIVRE (sem slug regex).

        O ``cli_model`` é o id nativo do CLI worker; sua validade depende do CLI
        de destino (catálogo do adapter), não da fronteira de wire. Aqui só
        garantimos que não vai vazio/whitespace (que viraria override falso) —
        ``None`` colapsado é dropado por ``model_dump(exclude_none=True)``.
        """
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("preferred_reasoning")
    @classmethod
    def _validate_reasoning(cls, v: Optional[str]) -> Optional[str]:
        """Reject unknown reasoning efforts at the wire boundary.

        ``None``/empty/whitespace colapsam para ``None``. Validamos contra a
        união de níveis conhecidos (:data:`deile.core.models.reasoning.KNOWN_EFFORTS`);
        o mapeamento por provider/worker (que sabe o que é suportado) acontece
        no consumidor com fail-open.
        """
        if v is None:
            return None
        stripped = v.strip().lower()
        if not stripped:
            return None
        from deile.core.models.reasoning import is_valid_effort
        if not is_valid_effort(stripped):
            raise ValueError(
                f"preferred_reasoning must be a known effort (got {stripped!r})"
            )
        return stripped

    @field_validator("stage")
    @classmethod
    def _validate_stage(cls, v: Optional[str]) -> Optional[str]:
        """Reject unknown stages at the wire boundary (issue #309 fase 2).

        ``PIPELINE_STAGES`` é importado no topo do módulo:
        :mod:`deile.orchestration.pipeline.dispatch_resolver` só depende da
        stdlib (``os`` / ``typing``), então não há ciclo de import.
        ``None``/empty/whitespace colapsam pra ``None`` (sem override de
        contexto de pipeline).
        """
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            return None
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
    # CLI-worker model override (frota multi-CLI) — string livre; omitida do
    # wire quando ``None``. Exclusivo de stages roteados para ``*-worker`` CLI.
    cli_model: Optional[str] = None,
    # --- Pipeline context (issue #309 fase 2) -------------------------------
    # Todos opcionais e adicionados ao FINAL para preservar a ordem dos kwargs
    # existentes (callers que dependem da assinatura por posição continuam
    # funcionando). Quando ``None`` (o default), a chave é dropada do payload
    # — mesma disciplina dos campos opcionais antigos — para garantir
    # backward compat com worker antigo que não conhece estes campos.
    stage: Optional[str] = None,
    action_kind: Optional[str] = None,
    issue_number: Optional[int] = None,
    branch: Optional[str] = None,
    # --- Resume context (issue #309 fase 3.5) -------------------------------
    # Quando ambos setados, o claude-worker spawna com ``-r <session_id>`` em
    # vez de ``--session-id <new_uuid>`` — claude retoma a conversa anterior
    # via JSONL persistido no PVC. Pipeline resolve via DispatchLedger.
    resume_session_id: Optional[str] = None,
    prev_task_id: Optional[str] = None,
    # --- Per-stage dispatch tuning (issue #391) -----------------------------
    timeout_s: Optional[int] = None,
    max_retries: Optional[int] = None,
    # Reasoning effort por etapa — omitido do wire quando ``None``.
    preferred_reasoning: Optional[str] = None,
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

    ``stage`` / ``action_kind`` / ``issue_number`` / ``branch`` (issue #309
    fase 2) carregam o contexto do pipeline para telemetry / log correlation
    no worker. São opcionais e omitidos do wire quando ``None`` — workers
    antigos que não conhecem estes campos continuam funcionando porque o
    cliente serializa via ``model_dump(exclude_none=True)``.

    ``timeout_s`` / ``max_retries`` (issue #391) carregam limites por-stage.
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
    if cli_model:
        payload["cli_model"] = str(cli_model)
    if preferred_reasoning:
        payload["preferred_reasoning"] = str(preferred_reasoning)
    if stage:
        payload["stage"] = str(stage)
    if action_kind:
        payload["action_kind"] = str(action_kind)
    if issue_number is not None:
        # ``issue_number`` é ``int``: ``if issue_number`` dropa 0
        # (não-issue válida, mas mesmo assim distinguir é correto).
        # Pydantic já valida ``ge=1`` na chegada, então 0 é rejeitado lá.
        payload["issue_number"] = int(issue_number)
    if branch:
        payload["branch"] = str(branch)
    if resume_session_id:
        payload["resume_session_id"] = str(resume_session_id)
    if prev_task_id:
        payload["prev_task_id"] = str(prev_task_id)
    if timeout_s is not None:
        payload["timeout_s"] = int(timeout_s)
    if max_retries is not None:
        payload["max_retries"] = int(max_retries)
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
        self,
        payload: Dict[str, Any],
        *,
        wait: bool,
        endpoint_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """POST a dispatch payload to the worker and return its parsed JSON body.

        Wraps :meth:`_dispatch_once` with the process-wide circuit breaker
        (issue #620 AC5) and exponential-backoff retry (AC4):

        - **Circuit breaker**: when 5 consecutive dispatches fail the circuit
          opens and the next dispatch fails fast with ``CIRCUIT_OPEN`` (no
          I/O); after 30s a single half-open probe is allowed.
        - **Retry**: only transient failures (``WORKER_TIMEOUT`` /
          ``WORKER_UNREACHABLE`` / HTTP >= 500) are retried, up to
          :data:`_RETRY_MAX_ATTEMPTS` total attempts, capped further by the
          payload's ``max_retries`` ceiling. 4xx (incl. 409/429) is never
          retried — the request itself is the problem.

        Raises :class:`WorkerDispatchError` — carrying an ``error_code`` — for
        every failure mode.

        Args:
            payload: dispatch body (validated against :class:`DispatchPayload`).
            wait: ``True`` para esperar o resultado (timeout longo,
                ``MAX_DISPATCH_BUDGET_S``); ``False`` para fire-and-forget
                (timeout curto, ``_NOWAIT_TIMEOUT_S``).
            endpoint_url: opcional, **issue #309 fase 2** — sobrescreve a
                URL base resolvida via ``DEILE_WORKER_ENDPOINT``. Quando
                setado (não-falsy), o POST vai para ``{endpoint_url}/v1/dispatch``
                em vez do default. Habilita o roteamento per-stage do
                pipeline (``WorkerImplementer._resolve_endpoint(stage)``
                aponta ``pr_review`` → claude-worker:8767 e ``implement`` →
                deile-worker:8766 no mesmo cliente). Ausência ou string
                vazia mantém o comportamento legacy (env var).
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

        # Circuit breaker gate (AC5): when OPEN inside the reset window we
        # fail fast without touching the network. ``allow`` flips OPEN →
        # HALF-OPEN once the window elapses, letting a single probe through.
        if not await _CIRCUIT_BREAKER.allow():
            raise WorkerDispatchError(
                "circuit breaker open — worker degraded, dispatch rejected",
                error_code="CIRCUIT_OPEN",
            )

        # Retry ceiling (AC4): the absolute max is ``_RETRY_MAX_ATTEMPTS``,
        # narrowed by the per-stage ``max_retries`` (0 = no retry). The first
        # call always runs, so ``max_attempts`` is the total attempt count.
        max_attempts = _RETRY_MAX_ATTEMPTS
        if validated.max_retries is not None:
            max_attempts = min(max_attempts, validated.max_retries + 1)

        last_exc: Optional[WorkerDispatchError] = None
        for attempt in range(1, max_attempts + 1):
            try:
                data = await self._dispatch_once(
                    body, wait=wait, endpoint_url=endpoint_url,
                )
            except WorkerDispatchError as exc:
                last_exc = exc
                if not exc.is_retryable() or attempt >= max_attempts:
                    await _CIRCUIT_BREAKER.record_failure()
                    raise
                delay = self._backoff_delay(attempt - 1)
                logger.warning(
                    "worker dispatch attempt %d/%d failed (%s); retrying in %.2fs",
                    attempt, max_attempts, exc.error_code, delay,
                )
                await asyncio.sleep(delay)
                continue
            else:
                await _CIRCUIT_BREAKER.record_success()
                return data

        # Unreachable in practice (the loop returns or raises), but keeps the
        # type checker happy and is defensive if max_attempts were ever 0.
        await _CIRCUIT_BREAKER.record_failure()
        raise last_exc or WorkerDispatchError(
            "dispatch exhausted with no attempt", error_code="WORKER_ERROR",
        )

    @staticmethod
    def _backoff_delay(retry_index: int) -> float:
        """Backoff exponencial com jitter para a tentativa ``retry_index`` (0-based).

        ``base * factor^retry_index`` mais jitter uniforme ``±jitter``;
        nunca negativo (issue #620 AC4).
        """
        delay = _RETRY_BASE_S * (_RETRY_FACTOR ** retry_index)
        delay += random.uniform(-_RETRY_JITTER_S, _RETRY_JITTER_S)
        return max(0.0, delay)

    async def _dispatch_once(
        self,
        body: Dict[str, Any],
        *,
        wait: bool,
        endpoint_url: Optional[str],
    ) -> Dict[str, Any]:
        """Single POST attempt against the worker (no retry, no breaker).

        ``body`` is already a validated/serialized dispatch payload. Raises
        :class:`WorkerDispatchError` carrying the wire ``error_code`` and,
        for HTTP responses, ``http_status`` so the retry layer can classify
        transient (5xx) vs definitive (4xx) failures.
        """
        # Issue #309 fase 2: ``endpoint_url`` (per-stage routing) sobrescreve
        # o resolver legacy quando truthy. Empty-string e ``None`` caem no
        # fallback de env var — paridade com ``_resolve_endpoint`` no
        # implementer, que considera unset == "use o default".
        base = endpoint_url if endpoint_url else _resolve_endpoint()
        endpoint = base.rstrip("/") + _DISPATCH_PATH
        # Token resolution touches secret files on disk — keep that blocking
        # I/O off the event loop. The token is a secret: it must never be
        # interpolated into log or error messages.
        token, httpx = await _resolve_auth_and_httpx()

        request_id = str(uuid.uuid4())
        # Structured timeout (NÃO um float escalar): no httpx, passar um float
        # único define connect/read/write/pool TODOS com o mesmo valor — com o
        # budget de 2h isso deixa um connect pendurado travar até 2h. Aqui o
        # ``read``/``write`` segue o budget do task (o worker pode legitimamente
        # levar até 2h), mas ``connect``/``pool`` ganham um teto curto.
        total_budget: float = MAX_DISPATCH_BUDGET_S if wait else _NOWAIT_TIMEOUT_S
        timeout = httpx.Timeout(
            total_budget,
            connect=min(_CONNECT_TIMEOUT_S, total_budget),
            pool=min(_POOL_TIMEOUT_S, total_budget),
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }
        # Issue #457 — D2: inject W3C traceparent from the current OTel context
        # so the worker can open deile.dispatch as a child of pipeline.dispatch_request.
        # This is the single injection point covering all call sites of _post_dispatch()
        # in implementer.py. Falls open when opentelemetry-api is not installed.
        try:
            from opentelemetry import propagate as _propagate  # noqa: PLC0415
            _propagate.inject(headers)
        except ImportError:
            pass
        logger.info(
            "worker dispatch starting",
            extra={"request_id": request_id, "wait": wait},
        )
        async with httpx.AsyncClient(timeout=timeout) as cli:
            try:
                resp = await cli.post(endpoint, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise WorkerDispatchError(
                    f"worker timeout after {total_budget}s "
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
                http_status=resp.status_code,
            ) from exc

        if resp.status_code >= 400:
            err_raw = data.get("error") if isinstance(data, dict) else None
            err = err_raw if isinstance(err_raw, dict) else {}
            code = (
                err.get("code")
                or (data.get("error_code") if isinstance(data, dict) else None)
                or "WORKER_ERROR"
            )
            msg = (
                err.get("message")
                or (err_raw if isinstance(err_raw, str) else None)
                or f"HTTP {resp.status_code}"
            )
            raise WorkerDispatchError(
                f"{msg} (request_id={request_id})",
                error_code=code,
                http_status=resp.status_code,
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

    async def get_resume_info(
        self,
        task_id: str,
        *,
        endpoint_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET /v1/dispatches/{task_id}/resume-info`` (claude-worker).

        Retorna metadata da sessão claude pra o pipeline decidir entre
        resume vs fresh dispatch. Mesmo error code map de :meth:`get_progress`
        (NOT_FOUND/WORKER_TIMEOUT/WORKER_UNREACHABLE/etc).

        Args:
            task_id: hex 16-char do dispatch original (worker valida).
            endpoint_url: opcional — base URL do worker (default = legacy
                env var). Necessário pra apontar pra claude-worker:8767
                quando o pipeline usa per-stage routing.
        """
        return await self._get_json(
            _RESUME_INFO_PATH.format(task_id=task_id),
            endpoint_url=endpoint_url,
        )

    async def _get_json(
        self,
        path: str,
        *,
        endpoint_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Helper compartilhado: GET autenticado + JSON parsing.

        Reusa a resolução de endpoint/token de :meth:`dispatch`, mas com
        timeout curto (polling, não dispatch). Erros mapeiam para os mesmos
        :class:`WorkerDispatchError` codes; o caller decide se é fatal.

        ``endpoint_url`` opcional — quando truthy, sobrescreve a resolução
        legacy via env var. Habilita GET contra claude-worker:8767 em
        per-stage routing (paridade com :meth:`dispatch`).
        """
        base = endpoint_url if endpoint_url else _resolve_endpoint()
        endpoint = base.rstrip("/") + path
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
            err_raw = data.get("error") if isinstance(data, dict) else None
            err = err_raw if isinstance(err_raw, dict) else {}
            code = (
                err.get("code")
                or (data.get("error_code") if isinstance(data, dict) else None)
                or "WORKER_BAD_REQUEST"
            )
            msg = (
                err.get("message")
                or (err_raw if isinstance(err_raw, str) else None)
                or f"HTTP {resp.status_code}"
            )
            raise WorkerDispatchError(msg, error_code=code)
        if not isinstance(data, dict):
            raise WorkerDispatchError(
                "worker returned non-object body",
                error_code="WORKER_BAD_RESPONSE",
            )
        return data
