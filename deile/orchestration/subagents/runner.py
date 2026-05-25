"""Runners de sub-DEILEs (issue #257).

Duas implementações atrás da mesma interface :class:`SubAgentRunner`:

* :class:`LocalSubAgentRunner` — disparo in-process. Cada sub-tarefa abre uma
  sessão limpa (``session_id`` próprio) e consome o stream de
  :meth:`DeileAgent.process_input_stream` para popular ``SubAgentState`` em
  tempo real. Sem dependência de infra; é o caminho default da CLI.

* :class:`WorkerSubAgentRunner` — delega ao ``deile-worker`` via
  :class:`deile.infrastructure.deile_worker_client.DeileWorkerClient` (com
  ``wait=False`` → ``task_id`` imediato + polling de ``GET /v1/progress/...``).
  Habilitado quando ``DEILE_SUBAGENT_RUNNER=worker`` (ou settings equivalente);
  útil quando o worker está alcançável (e.g. dentro do ``deile-shell`` pod).

A escolha é feita por :func:`resolve_runner` em runtime, lendo
``get_settings().subagent_runner``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Optional, Protocol

from ...common.text_utils import truncate
from ...common.tool_args import TOOL_PRIMARY_ARG_KEYS
from .events import SubAgentEvent, SubAgentEventKind, SubAgentState

logger = logging.getLogger(__name__)


# Callback que o orquestrador injeta em ``run_one`` — recebe cada evento
# emitido pelo runner. Não async para manter o caminho rápido (o renderer
# apenas guarda o snapshot e re-desenha no próximo frame).
OnEvent = Callable[[SubAgentEvent], None]


class SubAgentRunner(Protocol):
    """Interface pluggable para executar uma sub-tarefa.

    Implementações DEVEM:
      1. Atualizar ``state.status``, ``state.started_at``, ``state.finished_at``,
         ``state.result_text``, ``state.error`` e ``state.files_touched``;
      2. Chamar ``on_event(...)`` em milestones (STARTED, TOOL, TOOL_RESULT,
         TEXT, COMPLETED, FAILED) — o orquestrador repassa ao renderer;
      3. Capturar exceções e marcar ``state.status="error"`` em vez de propagar
         (o orquestrador drena ``task.exception()`` pós-``wait`` como rede de
         segurança, mas o caminho normal NÃO deve propagar).
    """

    async def run_one(
        self,
        state: SubAgentState,
        *,
        on_event: OnEvent,
    ) -> None:
        ...


# ---------- LocalSubAgentRunner -----------------------------------------------


# Tools cujo ``parsed_args["file_path"]`` indica que tocaram um arquivo —
# usado para popular ``state.files_touched`` em tempo real. Lista pequena
# e estável; outras tools de escrita aparecem via ``current_activity``.
_FILE_TOUCHING_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "patch_apply",
    "delete_file",
})


def _short(text: str, limit: int = 100) -> str:
    """Sanitize text for one-liner display (no newlines, bounded length).

    Thin wrapper around :func:`deile.common.text_utils.truncate` preserved for
    in-module call sites; central implementation lives in ``common`` to share
    semantics with ``deile/ui/subagent_panel._truncate``.
    """
    return truncate(text, limit, flatten_newlines=True)


class LocalSubAgentRunner:
    """Roda uma sub-tarefa via ``DeileAgent.process_input_stream`` no mesmo processo.

    Isolamento é de **contexto** (sessão própria, histórico próprio), não de
    filesystem — todos os sub-DEILEs compartilham o mesmo CWD. Esta é a opção
    default porque funciona em qualquer ambiente (laptop, pod, CI) sem depender
    de o ``deile-worker`` estar de pé.

    Notas:
      * ``persona`` e ``model`` são passados como ``persona_name`` /
        ``forced_model`` no kwargs de :meth:`process_input_stream` — o agente
        já honra essas chaves (ver ``_get_or_create_session`` + ``_select_*``).
      * Cada sub-DEILE recebe um ``session_id`` único (``subagent_<random>``)
        para garantir histórico/contexto limpo, sem interferir na conversa
        principal do usuário.
    """

    def __init__(self, agent: Any) -> None:
        # ``agent`` é o ``DeileAgent``. Tipo Any aqui para evitar import
        # circular — esta camada não deveria conhecer o tipo concreto.
        self._agent = agent

    async def run_one(
        self,
        state: SubAgentState,
        *,
        on_event: OnEvent,
    ) -> None:
        task = state.task
        state.status = "running"
        state.started_at = time.monotonic()
        on_event(SubAgentEvent(
            kind=SubAgentEventKind.STARTED,
            index=task.index,
            label=task.description,
        ))

        # session_id próprio garante contexto/histórico limpos. Não usamos
        # uuid completo pra manter logs legíveis. Limpamos a sessão no
        # ``finally`` para evitar RAM leak — agent._sessions é um dict que
        # cresceria sem limite após dispatches sucessivos.
        session_id = f"subagent_{task.index}_{uuid.uuid4().hex[:8]}"

        try:
            kwargs: dict = {"session_id": session_id}
            if task.persona:
                # ``persona_name`` é a chave canônica usada pelo worker_server.
                # Para o caminho local o efeito é limitado (o PersonaManager é
                # singleton — switch entre sub-tarefas causaria race condition
                # entre sub-DEILEs paralelos com personas diferentes), então
                # passamos no kwargs como hint mas NÃO ativamos persona real.
                # O caminho WorkerSubAgentRunner sim isola via processo separado.
                kwargs["persona_name"] = task.persona
                # M4 (PR #295 review): aviso visível para que o caller saiba
                # que persona ainda não é efetivamente honored pelo runner local.
                # Emite tanto no log quanto como evento progress para o painel.
                warn_msg = (
                    f"persona={task.persona!r} é apenas hint no LocalSubAgentRunner "
                    "(PersonaManager singleton — switching entre sub-DEILEs paralelos "
                    "causaria race). Use o WorkerSubAgentRunner para isolamento real."
                )
                logger.warning(
                    "LocalSubAgentRunner #%d: %s", task.index, warn_msg
                )
                on_event(SubAgentEvent(
                    kind=SubAgentEventKind.PROGRESS,
                    index=task.index,
                    label=f"⚠ persona={task.persona} (hint apenas — local runner)",
                ))
            if task.model:
                kwargs["forced_model"] = task.model

            # Envelope: sub-DEILE recebe contexto explícito de que é um
            # sub-agente despachado. Isso ajuda independente de persona — o
            # LLM trata o prompt como tarefa pontual sem inventar interação
            # com "usuário principal".
            prompt = (
                "[CONTEXTO] Você é um sub-DEILE despachado em paralelo a partir "
                "de um DEILE principal. Sessão limpa, contexto isolado. Foque "
                "exclusivamente no que está descrito a seguir. Trabalhe sozinho "
                "até concluir; não interaja com 'o usuário' — não há usuário "
                "aqui, só você e a tarefa.\n\n"
                f"[TAREFA]\n{task.prompt}"
            )

            # Importação tardia para evitar dependência circular (este módulo
            # é importado pelo pacote orchestration; o agent.py importa de cá).
            from deile.core.models.stream_events import StreamEventType

            stream = self._agent.process_input_stream(prompt, **kwargs)
            response_chunks: list[str] = []

            async for event in stream:
                # TEXT_DELTA → atividade "modelo respondendo: <primeira linha>"
                if event.type is StreamEventType.TEXT_DELTA and event.text:
                    response_chunks.append(event.text)
                    first_line = event.text.lstrip().splitlines()[0] if event.text.strip() else ""
                    if first_line:
                        label = f"✎ {_short(first_line)}"
                        state.current_activity = label
                        on_event(SubAgentEvent(
                            kind=SubAgentEventKind.TEXT,
                            index=task.index,
                            label=label,
                        ))

                # TOOL_USE_END → tool com args parseados (entrou em execução)
                elif event.type is StreamEventType.TOOL_USE_END:
                    tool_name = event.tool_name or "tool"
                    args = event.arguments or {}
                    detail = _format_tool_inline(tool_name, args)
                    label = f"⚙ {tool_name}({detail})" if detail else f"⚙ {tool_name}"
                    state.push_progress(label)
                    if tool_name in _FILE_TOUCHING_TOOLS:
                        state.add_file(args.get("file_path") or args.get("path"))
                    on_event(SubAgentEvent(
                        kind=SubAgentEventKind.TOOL,
                        index=task.index,
                        label=label,
                        tool_name=tool_name,
                    ))

                # TOOL_RESULT → flip status + summary curto
                elif event.type is StreamEventType.TOOL_RESULT:
                    tool_name = event.tool_name or "tool"
                    status = event.tool_status or "success"
                    summary = _short(event.tool_result_summary or "", 80)
                    glyph = "✓" if status == "success" else "✗"
                    line = f"{glyph} {tool_name}: {summary}" if summary else f"{glyph} {tool_name}"
                    state.push_progress(line)
                    # Capta file_path do metadata (write/edit gravaram lá).
                    meta = event.tool_metadata or {}
                    if isinstance(meta, dict):
                        state.add_file(meta.get("file_path"))
                    on_event(SubAgentEvent(
                        kind=SubAgentEventKind.TOOL_RESULT,
                        index=task.index,
                        label=line,
                        tool_name=tool_name,
                        tool_status=status,
                    ))

                # Outros tipos (USAGE_FINAL, STAGE, PROGRESS) — ignoramos no
                # painel; mantemos o stream consumindo até o fim.

            state.result_text = "".join(response_chunks).strip()
            state.status = "ok"
            state.finished_at = time.monotonic()
            on_event(SubAgentEvent(
                kind=SubAgentEventKind.COMPLETED,
                index=task.index,
                label="✅ concluído",
            ))

        except asyncio.CancelledError:
            _mark_cancelled(state, on_event)
            raise
        except Exception as exc:  # noqa: BLE001 — runner DEVE encapsular
            logger.exception("LocalSubAgentRunner: sub-tarefa #%d falhou", task.index)
            _mark_error(state, on_event, exc)
        finally:
            # Cleanup do sub-session — sem isso ``agent._sessions`` cresce
            # indefinidamente sob dispatches repetidos (revisão crítica B1).
            # iter-2 review: especifica exceções (KeyError/AttributeError —
            # session já removida ou agent sem o atributo); CancelledError
            # mid-cleanup precisa de log + re-raise (Pilar 03 §6).
            try:
                delete = getattr(self._agent, "delete_session", None)
                if callable(delete):
                    delete(session_id)
                else:
                    sessions = getattr(self._agent, "_sessions", None)
                    if isinstance(sessions, dict):
                        sessions.pop(session_id, None)
            except (KeyError, AttributeError):
                logger.debug(
                    "sub-session %s already cleaned or unavailable",
                    session_id,
                )
            except asyncio.CancelledError:
                logger.warning(
                    "LocalSubAgentRunner: cancel during session cleanup #%d",
                    task.index,
                )
                raise


def _mark_cancelled(state: SubAgentState, on_event: OnEvent) -> None:
    """Helper: marca um state como cancelled e emite o evento FAILED correspondente.

    Compartilhado entre :class:`LocalSubAgentRunner` e :class:`WorkerSubAgentRunner`
    para evitar drift entre os dois blocos ``except asyncio.CancelledError``.
    """
    state.status = "cancelled"
    state.finished_at = time.monotonic()
    on_event(SubAgentEvent(
        kind=SubAgentEventKind.FAILED,
        index=state.task.index,
        label="⏹ cancelado",
        error="cancelled",
    ))


def _mark_error(state: SubAgentState, on_event: OnEvent, exc: BaseException) -> str:
    """Helper: marca um state como error e emite o evento FAILED correspondente.

    Retorna a string ``err`` formatada para que o caller possa logá-la.
    """
    err = f"{type(exc).__name__}: {exc}"
    state.status = "error"
    state.error = err
    state.finished_at = time.monotonic()
    on_event(SubAgentEvent(
        kind=SubAgentEventKind.FAILED,
        index=state.task.index,
        label=f"✗ erro: {_short(err, 80)}",
        error=err,
    ))
    return err


def _format_tool_inline(tool_name: str, args: dict) -> str:
    """Format minimal argumento inline para o painel (≤60 chars)."""
    if not isinstance(args, dict) or not args:
        return ""
    # Tools de comando primário: mostra só o valor.
    pk = TOOL_PRIMARY_ARG_KEYS.get(tool_name)
    if pk and pk in args:
        return _short(str(args[pk]), 60)
    # Fallback: primeira chave string.
    for k, v in args.items():
        if isinstance(v, (str, int, float)):
            return _short(f"{k}={v}", 60)
    return ""


# ---------- WorkerSubAgentRunner ----------------------------------------------


class WorkerSubAgentRunner:
    """Roda uma sub-tarefa delegando ao ``deile-worker`` via HTTP.

    Útil quando o worker está alcançável (deile-shell pod, bridge local).
    O fluxo é:

      1. ``DeileWorkerClient.dispatch(wait=False)`` → ``task_id``.
      2. Loop de polling ``GET /v1/progress/{task_id}`` a cada
         ``poll_interval_s`` segundos, atualizando ``state.progress_lines`` /
         ``state.current_activity`` em tempo real.
      3. Quando o snapshot retorna ``ok ∈ {True, False}`` (terminal),
         busca-se o resultado completo via ``GET /v1/result/{task_id}`` para
         coletar ``files`` + ``summary``.

    O ``channel_id`` enviado é sintético (``cli:{session_id}``): o worker tenta
    postar a mensagem-status no Discord via :func:`_bot_facade`, mas ``None``
    é retornado quando o bot não está disponível e a degradação é silenciosa
    (ver ``infra/k8s/worker_server.py:138``).
    """

    def __init__(
        self,
        client: Any,
        *,
        session_id: str,
        poll_interval_s: float = 0.8,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._poll_interval_s = max(0.2, float(poll_interval_s))

    async def run_one(
        self,
        state: SubAgentState,
        *,
        on_event: OnEvent,
    ) -> None:
        task = state.task
        state.status = "running"
        state.started_at = time.monotonic()
        on_event(SubAgentEvent(
            kind=SubAgentEventKind.STARTED,
            index=task.index,
            label=task.description,
        ))

        try:
            from deile.infrastructure.deile_worker_client import (
                WorkerDispatchError, build_dispatch_payload)

            # Synthetic channel_id — bot integration silently no-ops when the
            # facade is unavailable (worker_server.py:_bot_facade returns None).
            channel_id = f"cli:{self._session_id}:{task.index}"
            persona = task.persona or "developer"
            payload = build_dispatch_payload(
                brief=task.prompt,
                channel_id=channel_id,
                persona=persona,
                wait=False,
            )
            data = await self._client.dispatch(payload, wait=False)
            task_id = data.get("task_id") if isinstance(data, dict) else None
            if not task_id:
                raise WorkerDispatchError(
                    "worker did not return a task_id",
                    error_code="WORKER_BAD_RESPONSE",
                )
            state.task_id = task_id
            on_event(SubAgentEvent(
                kind=SubAgentEventKind.PROGRESS,
                index=task.index,
                label=f"task_id={task_id}",
            ))

            seen_lines = 0
            while True:
                await asyncio.sleep(self._poll_interval_s)
                try:
                    snap = await self._client.get_progress(task_id)
                except WorkerDispatchError as exc:
                    # 404 transiente pode acontecer logo após o dispatch;
                    # esperamos um tick e seguimos.
                    if exc.error_code == "NOT_FOUND":
                        continue
                    raise

                if not isinstance(snap, dict):
                    continue

                # Atualiza progress_lines incrementalmente — só as linhas
                # novas em relação ao último snapshot.
                lines = snap.get("progress_lines") or []
                if isinstance(lines, list) and len(lines) > seen_lines:
                    for line in lines[seen_lines:]:
                        if isinstance(line, str):
                            state.push_progress(line)
                            on_event(SubAgentEvent(
                                kind=SubAgentEventKind.PROGRESS,
                                index=task.index,
                                label=line[:120],
                            ))
                    seen_lines = len(lines)

                current = snap.get("current_activity")
                if isinstance(current, str) and current:
                    state.current_activity = current[:120]

                ok = snap.get("ok")
                if ok is not None:
                    # Terminal — busca resultado completo.
                    try:
                        result = await self._client.get_result(task_id)
                    except WorkerDispatchError:
                        result = snap  # fallback ao último snapshot
                    if isinstance(result, dict):
                        files = result.get("files") or []
                        if isinstance(files, list):
                            for f in files:
                                state.add_file(f)
                        state.result_text = _short(
                            str(result.get("summary") or ""), 4000
                        )
                    if ok is True:
                        state.status = "ok"
                        state.finished_at = time.monotonic()
                        on_event(SubAgentEvent(
                            kind=SubAgentEventKind.COMPLETED,
                            index=task.index,
                            label="✅ concluído",
                        ))
                    else:
                        state.status = "error"
                        state.error = (snap.get("error") or "worker error")
                        state.finished_at = time.monotonic()
                        on_event(SubAgentEvent(
                            kind=SubAgentEventKind.FAILED,
                            index=task.index,
                            label=f"✗ erro: {_short(state.error, 80)}",
                            error=state.error,
                        ))
                    return

        except asyncio.CancelledError:
            _mark_cancelled(state, on_event)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("WorkerSubAgentRunner: sub-tarefa #%d falhou", task.index)
            _mark_error(state, on_event, exc)


# ---------- Factory -----------------------------------------------------------


def resolve_runner(
    agent: Any,
    *,
    session_id: str,
    runner_kind: Optional[str] = None,
    poll_interval_s: Optional[float] = None,
) -> SubAgentRunner:
    """Resolve qual runner usar.

    Args:
        agent: o ``DeileAgent`` (passado ao :class:`LocalSubAgentRunner`).
        session_id: identifica a sessão CLI principal (usado como prefixo do
            ``channel_id`` sintético no worker runner).
        runner_kind: override explícito (``"local"`` ou ``"worker"``); quando
            ``None`` consulta ``get_settings().subagent_runner``.
        poll_interval_s: período de polling para o worker runner; quando
            ``None`` consulta ``get_settings().subagent_poll_interval_s``.

    Returns:
        :class:`SubAgentRunner` apropriado.
    """
    from deile.config.settings import get_settings

    settings = get_settings()
    kind = (runner_kind or getattr(settings, "subagent_runner", "local") or "local").lower()
    poll = float(
        poll_interval_s
        if poll_interval_s is not None
        else getattr(settings, "subagent_poll_interval_s", 0.8)
    )

    if kind == "worker":
        try:
            from deile.infrastructure.deile_worker_client import \
                DeileWorkerClient

            return WorkerSubAgentRunner(
                DeileWorkerClient(),
                session_id=session_id,
                poll_interval_s=poll,
            )
        except ImportError:
            logger.warning(
                "subagent_runner=worker requested but DeileWorkerClient import failed; "
                "falling back to LocalSubAgentRunner"
            )

    return LocalSubAgentRunner(agent)


__all__ = [
    "LocalSubAgentRunner",
    "OnEvent",
    "SubAgentRunner",
    "WorkerSubAgentRunner",
    "resolve_runner",
]
