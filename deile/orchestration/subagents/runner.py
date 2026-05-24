"""Runners de sub-DEILEs (issue #257).

Duas implementações atrás da mesma interface :class:`SubAgentRunner`:

* :class:`LocalSubAgentRunner` — disparo in-process. Cada sub-tarefa abre uma
  sessão limpa (``session_id`` próprio) e consome o stream de
  :meth:`DeileAgent.process_input_stream` para popular ``SubAgentState`` em
  tempo real. Default da CLI.

* :class:`WorkerSubAgentRunner` — delega ao ``deile-worker`` via HTTP
  (``wait=False`` → ``task_id`` imediato + polling de ``GET /v1/progress/...``).
  Habilitado quando ``DEILE_SUBAGENT_RUNNER=worker``.

Ambos compartilham :class:`_BaseRunner` que cobre o lifecycle (status →
running, started_at, STARTED event; try/except/finally para mark_cancelled /
mark_error / cleanup). Cada subclasse só implementa ``_do_work``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Optional, Protocol

from .events import SubAgentEvent, SubAgentEventKind, SubAgentState

logger = logging.getLogger(__name__)


# Callback síncrono que o orquestrador injeta — renderer só atualiza snapshot.
OnEvent = Callable[[SubAgentEvent], None]


class SubAgentRunner(Protocol):
    """Interface pluggable para executar uma sub-tarefa.

    Implementações DEVEM:
      1. Atualizar ``state.status``/``started_at``/``finished_at``/``result_text``/
         ``error``/``files_touched``;
      2. Emitir ``on_event(...)`` em milestones (STARTED, TOOL, TOOL_RESULT,
         TEXT, COMPLETED, FAILED);
      3. Encapsular exceções e marcar ``state.status="error"`` em vez de propagar.
    """

    async def run_one(
        self,
        state: SubAgentState,
        *,
        on_event: OnEvent,
    ) -> None:
        ...


_FILE_TOUCHING_TOOLS = frozenset({
    "write_file", "edit_file", "patch_apply", "delete_file",
})


def _short(text: str, limit: int = 100) -> str:
    """Sanitize text for one-liner display (no newlines, bounded length)."""
    if text is None:
        return ""
    s = str(text).replace("\n", " ⏎ ").replace("\r", " ").strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _mark_cancelled(state: SubAgentState, on_event: OnEvent) -> None:
    state.status = "cancelled"
    state.finished_at = time.monotonic()
    on_event(SubAgentEvent(
        kind=SubAgentEventKind.FAILED,
        index=state.task.index,
        label="⏹ cancelado",
        error="cancelled",
    ))


def _mark_error(state: SubAgentState, on_event: OnEvent, exc: BaseException) -> str:
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


class _BaseRunner:
    """Template method: trata status flips + cancel/error/finally; subclasses só fazem trabalho."""

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
            await self._do_work(state, on_event=on_event)
        except asyncio.CancelledError:
            _mark_cancelled(state, on_event)
            raise
        except Exception as exc:  # noqa: BLE001 — runner DEVE encapsular
            logger.exception("%s: sub-tarefa #%d falhou",
                             self.__class__.__name__, task.index)
            _mark_error(state, on_event, exc)
        finally:
            self._finalize(state)

    async def _do_work(self, state: SubAgentState, *, on_event: OnEvent) -> None:
        raise NotImplementedError

    def _finalize(self, state: SubAgentState) -> None:
        """Hook para cleanup (e.g. delete sub-session). Default no-op."""


# ---------- LocalSubAgentRunner -----------------------------------------------


class LocalSubAgentRunner(_BaseRunner):
    """Roda uma sub-tarefa via ``DeileAgent.process_input_stream`` no mesmo processo.

    Isolamento é de **contexto** (sessão própria, histórico próprio), não de
    filesystem — todos os sub-DEILEs compartilham o mesmo CWD.

    Notas:
      * ``persona``/``model`` viram ``persona_name``/``forced_model`` no kwargs;
        no caminho local persona é apenas hint (PersonaManager é singleton —
        switch entre sub-DEILEs paralelos causaria race). Use ``WorkerSubAgentRunner``
        para isolamento real.
      * Cada sub-DEILE recebe ``session_id`` único (``subagent_<n>_<rand>``).
    """

    def __init__(self, agent: Any) -> None:
        # ``Any`` evita import circular (agent.py importa de cá).
        self._agent = agent
        self._session_id: Optional[str] = None

    async def _do_work(self, state: SubAgentState, *, on_event: OnEvent) -> None:
        task = state.task
        self._session_id = f"subagent_{task.index}_{uuid.uuid4().hex[:8]}"

        # _skip_autonomous=True — bypass fast-path autônoma; sem isso o painel
        # fica mudo enquanto o sub trabalha.
        kwargs: dict = {"session_id": self._session_id, "_skip_autonomous": True}
        if task.persona:
            kwargs["persona_name"] = task.persona
            warn_msg = (
                f"persona={task.persona!r} é apenas hint no LocalSubAgentRunner "
                "(PersonaManager singleton — switching entre sub-DEILEs paralelos "
                "causaria race). Use o WorkerSubAgentRunner para isolamento real."
            )
            logger.warning("LocalSubAgentRunner #%d: %s", task.index, warn_msg)
            on_event(SubAgentEvent(
                kind=SubAgentEventKind.PROGRESS,
                index=task.index,
                label=f"⚠ persona={task.persona} (hint apenas — local runner)",
            ))
        if task.model:
            kwargs["forced_model"] = task.model

        prompt = (
            "[CONTEXTO] Você é um sub-DEILE despachado em paralelo a partir "
            "de um DEILE principal. Sessão limpa, contexto isolado. Foque "
            "exclusivamente no que está descrito a seguir. Trabalhe sozinho "
            "até concluir; não interaja com 'o usuário' — não há usuário "
            "aqui, só você e a tarefa.\n\n"
            f"[TAREFA]\n{task.prompt}"
        )

        # Importação tardia: evita ciclo deile.orchestration ↔ deile.core.
        from deile.core.models.stream_events import StreamEventType

        stream = self._agent.process_input_stream(prompt, **kwargs)
        response_chunks: list[str] = []

        async for event in stream:
            etype = event.type
            if etype is StreamEventType.TEXT_DELTA and event.text:
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
            elif etype is StreamEventType.TOOL_USE_END:
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
            elif etype is StreamEventType.TOOL_RESULT:
                tool_name = event.tool_name or "tool"
                status = event.tool_status or "success"
                summary = _short(event.tool_result_summary or "", 80)
                glyph = "✓" if status == "success" else "✗"
                line = f"{glyph} {tool_name}: {summary}" if summary else f"{glyph} {tool_name}"
                state.push_progress(line)
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
            # Outros tipos (USAGE_FINAL, STAGE, PROGRESS) ignorados no painel.

        state.result_text = "".join(response_chunks).strip()
        state.status = "ok"
        state.finished_at = time.monotonic()
        on_event(SubAgentEvent(
            kind=SubAgentEventKind.COMPLETED,
            index=task.index,
            label="✅ concluído",
        ))

    def _finalize(self, state: SubAgentState) -> None:
        # Cleanup do sub-session — agent._sessions é dict, cresce sem cleanup.
        sid = self._session_id
        if not sid:
            return
        try:
            delete = getattr(self._agent, "delete_session", None)
            if callable(delete):
                delete(sid)
            else:
                sessions = getattr(self._agent, "_sessions", None)
                if isinstance(sessions, dict):
                    sessions.pop(sid, None)
        except (KeyError, AttributeError):
            logger.debug("sub-session %s already cleaned or unavailable", sid)
        except asyncio.CancelledError:
            logger.warning(
                "LocalSubAgentRunner: cancel during session cleanup #%d",
                state.task.index,
            )
            raise


def _format_tool_inline(tool_name: str, args: dict) -> str:
    """Format minimal argumento inline para o painel (≤60 chars)."""
    if not isinstance(args, dict) or not args:
        return ""
    primary_keys = {
        "bash_execute": "command",
        "python_execute": "code",
        "read_file": "file_path",
        "write_file": "file_path",
        "list_files": "path",
        "delete_file": "file_path",
        "edit_file": "file_path",
    }
    pk = primary_keys.get(tool_name)
    if pk and pk in args:
        return _short(str(args[pk]), 60)
    for k, v in args.items():
        if isinstance(v, (str, int, float)):
            return _short(f"{k}={v}", 60)
    return ""


# ---------- WorkerSubAgentRunner ----------------------------------------------


class WorkerSubAgentRunner(_BaseRunner):
    """Roda uma sub-tarefa delegando ao ``deile-worker`` via HTTP.

    Fluxo:
      1. ``DeileWorkerClient.dispatch(wait=False)`` → ``task_id``.
      2. Loop ``GET /v1/progress/{task_id}`` a cada ``poll_interval_s`` segundos,
         atualizando ``state.progress_lines``/``current_activity``.
      3. Em terminal (``ok ∈ {True, False}``), busca ``GET /v1/result/{task_id}``
         para ``files`` + ``summary``.

    ``channel_id`` é sintético (``cli:{session_id}``); o worker no-op-a a integração
    com bot quando facade não disponível.
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

    async def _do_work(self, state: SubAgentState, *, on_event: OnEvent) -> None:
        from deile.infrastructure.deile_worker_client import (
            WorkerDispatchError, build_dispatch_payload)

        task = state.task
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
                # 404 transiente logo após dispatch: aguarda um tick e segue.
                if exc.error_code == "NOT_FOUND":
                    continue
                raise

            if not isinstance(snap, dict):
                continue

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
            if ok is None:
                continue

            # Terminal — busca resultado completo (fallback ao snap se falhar).
            try:
                result = await self._client.get_result(task_id)
            except WorkerDispatchError:
                result = snap
            if isinstance(result, dict):
                files = result.get("files") or []
                if isinstance(files, list):
                    for f in files:
                        state.add_file(f)
                state.result_text = _short(str(result.get("summary") or ""), 4000)
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
                state.error = snap.get("error") or "worker error"
                state.finished_at = time.monotonic()
                on_event(SubAgentEvent(
                    kind=SubAgentEventKind.FAILED,
                    index=task.index,
                    label=f"✗ erro: {_short(state.error, 80)}",
                    error=state.error,
                ))
            return


# ---------- Factory -----------------------------------------------------------


def resolve_runner(
    agent: Any,
    *,
    session_id: str,
    runner_kind: Optional[str] = None,
    poll_interval_s: Optional[float] = None,
) -> SubAgentRunner:
    """Resolve qual runner usar — lê ``get_settings().subagent_runner`` por default."""
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
            from deile.infrastructure.deile_worker_client import DeileWorkerClient
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
