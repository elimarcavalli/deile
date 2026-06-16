"""Provider-agnostic tool-loop executor for streaming turns.

Single source of truth for the iterative function-calling loop. Each provider
exposes ``generate_stream(tools=...)`` (which only emits events — never
executes tools) plus the two shape adapters
``format_assistant_tool_use_message`` and ``format_tool_result_message``;
``ToolLoopExecutor`` orchestrates iteration, registry execution, and
``TOOL_RESULT`` emission.

This file is the deduplication target for the previous per-provider
``chat_with_tools`` copies.
"""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from deile.core.loop_guard import (
    ToolLoopGuard,
    format_loop_break_message,
    make_guard,
    tool_result_made_progress,
)
from deile.core.models.base import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    ModelMessage,
    ModelProvider,
)
from deile.core.models.stream_events import StreamEventType, UnifiedStreamEvent
from deile.core.models.tool_execution import (
    OUTCOME_EXCEPTION,
    OUTCOME_RAN,
    build_tool_result_payload,
)
from deile.core.tool_result_summary import summarize
from deile.core.tool_scenario_kwargs import build_tool_stage_kwargs
from deile.tools.base import ToolContext, ToolResult, ToolStatus
from deile.tools.registry import ToolRegistry, get_tool_registry
from deile.ui.stage_cascade import cascade_stream, cascade_until
from deile.ui.stage_messages import get_stage_message  # noqa: F401

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = DEFAULT_MAX_TOOL_ITERATIONS


def _set_tool_span_status(span: Any, is_success: bool) -> None:
    """Marca o span da tool como OK/ERROR + ``deile.tool.result.status``."""
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode  # noqa: PLC0415

        span.set_attribute(
            "deile.tool.result.status", "success" if is_success else "error"
        )
        if not is_success:
            span.set_status(Status(StatusCode.ERROR))
    except Exception:  # noqa: BLE001 — observability nunca quebra a loop
        pass


def _set_tool_span_error(span: Any, exc: BaseException) -> None:
    """Marca ERROR + grava exception event no span da tool."""
    if span is None:
        return
    try:
        from opentelemetry.trace import Status, StatusCode  # noqa: PLC0415

        span.set_attribute("deile.tool.result.status", "error")
        span.set_status(Status(StatusCode.ERROR, description=type(exc).__name__))
        span.record_exception(exc)
        span.add_event(
            "deile.tool.error",
            attributes={
                "error.type": type(exc).__name__,
                "error.message": str(exc)[:200],
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _record_tool_metrics(tool_name: str, status: str, t0: float) -> None:
    """Emite ``deile.tool.duration_ms``."""
    try:
        from deile.observability import get_metrics  # noqa: PLC0415

        get_metrics().record_tool_duration(
            tool_name=tool_name,
            status=status,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
    except Exception:  # noqa: BLE001
        pass


def _resolve_max_iterations() -> int:
    """Configured tool-loop cap (settings/env), falling back to the constant.

    Resolved at executor construction so ``DEILE_MAX_TOOL_ITERATIONS`` /
    settings.json ``agent.max_tool_iterations`` take effect without a code
    change. Config must never break the loop — any failure falls back to the
    module constant.
    """
    try:
        from deile.config.settings import get_settings

        value = int(getattr(get_settings(), "max_tool_iterations", MAX_TOOL_ITERATIONS))
        return value if value > 0 else MAX_TOOL_ITERATIONS
    except Exception:  # noqa: BLE001 — config errors must not disable tool use
        logger.debug(
            "max_tool_iterations config unavailable; using default %d",
            MAX_TOOL_ITERATIONS,
            exc_info=True,
        )
        return MAX_TOOL_ITERATIONS


class ToolLoopExecutor:
    """Run a multi-iteration tool-use loop against any provider.

    The executor never decides *what* to call — the model does. It only:

    * Forwards every ``UnifiedStreamEvent`` from the provider to its consumer
      (UI, aggregator, anything).
    * Collects ``TOOL_USE_END`` events to know which tools the model wants to
      run after the round closes.
    * Executes those tools via the registry, emits ``TOOL_RESULT`` events,
      appends the result message to the rolling history, and re-invokes the
      provider for the next iteration.
    """

    def __init__(
        self,
        tool_registry: Optional[ToolRegistry] = None,
        max_iterations: Optional[int] = None,
        event_publisher: Optional[Any] = None,
        loop_guard: Optional[ToolLoopGuard] = None,
    ) -> None:
        self._tool_registry = tool_registry or get_tool_registry()
        # None → resolve from settings (DEILE_MAX_TOOL_ITERATIONS /
        # agent.max_tool_iterations); an explicit value (e.g. from tests) wins.
        self._max_iterations = (
            max_iterations if max_iterations is not None else _resolve_max_iterations()
        )
        self._event_publisher = (
            event_publisher  # callable: (kind, name, **kw) -> awaitable
        )
        # Each ``run()`` invocation builds its own guard (one per turn). The
        # constructor accepts an explicit guard only so tests can inject a
        # pre-configured detector — production callers leave it as ``None``.
        self._loop_guard_override = loop_guard

    async def run(
        self,
        provider: ModelProvider,
        messages: List[ModelMessage],
        tools: List[Any],
        system_instruction: Optional[str] = None,
        working_directory: str = ".",
        session_data: Optional[Dict[str, Any]] = None,
        reasoning_effort: Optional[str] = None,
    ) -> AsyncIterator[UnifiedStreamEvent]:
        """Stream the full tool-loop end-to-end.

        Yields every event the provider emits, plus ``TOOL_RESULT`` events
        produced by this executor for each tool the registry runs.

        ``reasoning_effort`` (quando setado) é repassado a
        ``provider.generate_stream`` em cada iteração; cada provider traduz o
        nível para o parâmetro nativo (best-effort). ``None`` = default do provider.
        """
        history = list(messages)
        # Per-turn loop detector. Defensive against the model spinning on
        # the same call when its previous tool returned an error or empty
        # data — see deile.core.loop_guard for the detection rules.
        guard = self._loop_guard_override or make_guard(
            session_id=str((session_data or {}).get("session_id", "")) or None,
        )

        for iteration in range(self._max_iterations):
            pending_tool_calls: List[Tuple[str, str, Dict[str, Any]]] = []
            text_so_far_parts: List[str] = []
            error_seen = False
            last_error_envelope: Optional[Any] = None
            # Captured from TOOL_USE_END events — providers that use reasoning/thinking
            # mode (e.g. DeepSeek-R1) require this to be echoed verbatim in the next
            # API call's assistant message, otherwise they return HTTP 400.
            last_reasoning_content: Optional[str] = None

            # Round-trip latency before the model starts streaming the next
            # iteration is otherwise silent — surface it as a STAGE cascade
            # so the UI keeps evolving (3s → 10s → 30s) until the first event.
            stream_iter = provider.generate_stream(
                history,
                system_instruction=system_instruction,
                tools=tools,
                reasoning_effort=reasoning_effort,
            )
            cascade_key = (
                "await_first_token" if iteration == 0 else "await_next_response"
            )
            cascade_ctx: Dict[str, Any] = (
                {} if iteration == 0 else {"iteration": str(iteration + 1)}
            )
            async for event in cascade_stream(
                stream_iter,
                message_key=cascade_key,
                event_iteration=iteration,
                **cascade_ctx,
            ):
                event.iteration = iteration
                yield event

                if event.type is StreamEventType.TEXT_DELTA and event.text:
                    text_so_far_parts.append(event.text)
                elif event.type is StreamEventType.TOOL_USE_END:
                    pending_tool_calls.append(
                        (
                            event.tool_call_id or "",
                            event.tool_name or "",
                            event.arguments or {},
                        )
                    )
                    if event.reasoning_content:
                        last_reasoning_content = event.reasoning_content
                elif event.type is StreamEventType.ERROR:
                    error_seen = True
                    last_error_envelope = event.error_envelope

            if error_seen:
                # Provider emitted ERROR — emit a user-friendly message for
                # context_length_exceeded, then abort the loop.
                if (
                    last_error_envelope is not None
                    and getattr(last_error_envelope, "error_type", None)
                    == "context_length_exceeded"
                ):
                    model_id = getattr(last_error_envelope, "model_id", "modelo")
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TEXT_DELTA,
                        text=(
                            f"O histórico desta conversa excedeu o limite de contexto do modelo **{model_id}**.\n\n"
                            "**Como resolver:**\n"
                            "• `/clear` — limpa o histórico e inicia uma nova sessão\n"
                            "• `/model select` — escolha um modelo com janela de contexto maior\n"
                            "• Divida sua pergunta em partes menores e mais objetivas\n"
                        ),
                    )
                return

            if not pending_tool_calls:
                # Model produced its final answer — terminate cleanly.
                return

            # Persist the assistant turn that requested the tools.
            text_so_far = "".join(text_so_far_parts)
            history.append(
                provider.format_assistant_tool_use_message(
                    pending_tool_calls,
                    text_so_far=text_so_far,
                    reasoning_content=last_reasoning_content,
                )
            )

            # Execute each tool sequentially, emitting TOOL_RESULT events.
            for tc_id, tc_name, tc_args in pending_tool_calls:
                tool_key, tool_kwargs = build_tool_stage_kwargs(tc_name, tc_args)

                # ── Loop guard: detect identical-call / windowed / no-progress
                # spirals before we burn another round-trip. If the guard
                # trips, emit a synthetic TOOL_RESULT (so the UI sees what
                # happened) plus a TEXT_DELTA explaining the abort, then
                # stop the entire run — the model has been demonstrably
                # going in circles.
                abort = guard.check(tc_name, tc_args)
                if abort is not None:
                    summary = format_loop_break_message(abort)
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TOOL_RESULT,
                        tool_call_id=tc_id,
                        tool_name=tc_name,
                        tool_status="error",
                        tool_result_summary=summary[:200],
                        tool_result_data=None,
                        tool_metadata={
                            "function_name": tc_name,
                            "tool_call_id": tc_id,
                            "loop_break": True,
                            "loop_break_kind": abort.kind.value,
                            "loop_break_args_hash": abort.args_hash,
                        },
                        iteration=iteration,
                    )
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TEXT_DELTA,
                        text=abort.user_message(),
                        source="loop_guard",
                    )
                    await self._publish(
                        "failed",
                        tc_name,
                        tc_id=tc_id,
                        error=summary,
                        loop_break=True,
                    )
                    return

                await self._publish("invoked", tc_name, tc_id=tc_id, args=tc_args)

                ctx = ToolContext(
                    user_input="",
                    parsed_args=dict(tc_args or {}),
                    session_data=dict(session_data or {}),
                    working_directory=working_directory or ".",
                    file_list=[],
                    metadata={
                        "execution_method": "tool_loop_executor",
                        "function_name": tc_name,
                        "tool_call_id": tc_id,
                    },
                )

                # Issue #303 — publica ação atual no runtime state (best-effort).
                # tc_name é safe (não args); session_id vem do session_data
                # passado pelo agente. Não falha o turn se o state file estiver
                # corrompido/ausente.
                _istate = None
                try:
                    from deile.runtime.instance_state import get_instance_state

                    _istate = get_instance_state()
                    _istate.update_action(
                        "tool_execution",
                        detail=tc_name,
                        session_id=str((session_data or {}).get("session_id", ""))
                        or None,
                    )
                except Exception:  # noqa: BLE001 — observability nunca quebra a loop
                    _istate = None

                # Issue #303 fase 4 — span filho ``deile.tool.<name>`` + métrica
                # de duração. Best-effort: spans/métricas nunca quebram a loop.
                _tool_span_cm: Any = None
                _tool_span: Any = None
                try:
                    from deile.observability import get_tracer

                    _tool_span_cm = get_tracer().tool(
                        tc_name,
                        args_size=len(str(tc_args or {})),
                    )
                    _tool_span = _tool_span_cm.__enter__()
                except Exception:  # noqa: BLE001
                    _tool_span_cm = None
                    _tool_span = None
                _tool_t0 = time.monotonic()

                # Run the tool under a temporal cascade so the user sees the
                # spinner text evolve when the tool takes >3s, >10s, >30s.
                # cascade_until yields STAGE events while the awaitable runs
                # and a final ("result", value) tuple when it completes; it
                # re-raises the underlying exception if the tool fails.
                result: Any = None
                try:
                    try:
                        async for item in cascade_until(
                            self._tool_registry.execute_tool(tc_name, ctx),
                            message_key=tool_key,
                            event_iteration=iteration,
                            **tool_kwargs,
                        ):
                            if isinstance(item, tuple) and item and item[0] == "result":
                                result = item[1]
                            elif isinstance(item, UnifiedStreamEvent):
                                yield item
                    except Exception as exc:  # pylint: disable=broad-except
                        logger.error(
                            "Tool '%s' raised in ToolLoopExecutor: %s",
                            tc_name,
                            exc,
                            exc_info=True,
                        )
                        # Issue #303 fase 4 — span ERROR + métrica.
                        _set_tool_span_error(_tool_span, exc)
                        _record_tool_metrics(tc_name, "error", _tool_t0)
                        err_result = ToolResult(
                            status=ToolStatus.ERROR,
                            message=f"{type(exc).__name__}: {exc}",
                            error=exc,
                            metadata={"function_name": tc_name, "tool_call_id": tc_id},
                        )
                        yield UnifiedStreamEvent(
                            type=StreamEventType.TOOL_RESULT,
                            tool_call_id=tc_id,
                            tool_name=tc_name,
                            tool_status="error",
                            tool_result_summary=summarize(err_result),
                            tool_result_data=None,
                            tool_metadata=dict(err_result.metadata or {}),
                            iteration=iteration,
                        )
                        history.append(
                            provider.format_tool_result_message(
                                tc_id,
                                tc_name,
                                build_tool_result_payload(
                                    err_result,
                                    OUTCOME_EXCEPTION,
                                    tc_name,
                                    include_message=True,
                                    include_data_on_error=True,
                                ),
                            )
                        )
                        # An exception escaping the registry is always "no progress"
                        # — feed that into the guard so a string of failures
                        # eventually trips the no-progress rule.
                        guard.record_result(made_progress=False)
                        if _istate is not None:
                            try:
                                _istate.update_stats(tool_calls=1, errors=1)
                            except Exception:  # noqa: BLE001
                                pass
                        await self._publish("failed", tc_name, tc_id=tc_id, error=exc)
                        continue

                    if result.metadata is None:
                        result.metadata = {}
                    result.metadata.setdefault("function_name", tc_name)
                    result.metadata.setdefault("tool_call_id", tc_id)

                    # Issue #303 fase 4 — span status + métrica de duração.
                    _set_tool_span_status(_tool_span, result.is_success)
                    _record_tool_metrics(
                        tc_name,
                        "success" if result.is_success else "error",
                        _tool_t0,
                    )

                    yield UnifiedStreamEvent(
                        type=StreamEventType.TOOL_RESULT,
                        tool_call_id=tc_id,
                        tool_name=tc_name,
                        tool_status="success" if result.is_success else "error",
                        tool_result_summary=summarize(result),
                        tool_result_data=result.data,
                        tool_metadata=dict(result.metadata or {}),
                        iteration=iteration,
                    )
                    history.append(
                        provider.format_tool_result_message(
                            tc_id,
                            tc_name,
                            build_tool_result_payload(
                                result,
                                OUTCOME_RAN,
                                tc_name,
                                include_message=True,
                                include_data_on_error=True,
                            ),
                        )
                    )
                    # Feed the result into the guard so the no-progress rule can
                    # observe consecutive empty/error returns.
                    guard.record_result(made_progress=tool_result_made_progress(result))
                    if _istate is not None:
                        try:
                            _istate.update_stats(
                                tool_calls=1,
                                errors=0 if result.is_success else 1,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    await self._publish(
                        "completed" if result.is_success else "failed",
                        tc_name,
                        tc_id=tc_id,
                        success=result.is_success,
                    )
                finally:
                    # Issue #303 — restaura o estado depois de cada tool. O
                    # ``_stream_chat_with_tools`` reaplica ``llm_call`` no
                    # próximo ciclo do gerador antes de retornar à LLM.
                    if _istate is not None:
                        try:
                            _istate.clear_action()
                        except Exception:  # noqa: BLE001
                            pass
                    # Issue #303 fase 4 — fecha o span ``deile.tool.<name>``.
                    if _tool_span_cm is not None:
                        try:
                            _tool_span_cm.__exit__(None, None, None)
                        except Exception:  # noqa: BLE001
                            pass

        yield UnifiedStreamEvent(
            type=StreamEventType.STAGE,
            stage=get_stage_message(
                "max_iterations", "initial", max=self._max_iterations
            ),
        )
        logger.warning(
            "ToolLoopExecutor: hit max_iterations=%d for provider=%s",
            self._max_iterations,
            getattr(provider, "provider_id", "?"),
        )

    async def _publish(self, kind: str, tool_name: str, **kw: Any) -> None:
        """Best-effort fanout to the event publisher; never raises."""
        if self._event_publisher is None:
            return
        try:
            await self._event_publisher(kind, tool_name, **kw)
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug(
                "event_publisher failed (kind=%s, tool=%s): %s", kind, tool_name, exc
            )
