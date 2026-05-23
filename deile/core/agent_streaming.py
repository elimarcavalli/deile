"""Streaming methods extracted from DeileAgent.

This mixin owns the streaming-path methods (``process_input_stream``,
``_stream_chat_with_tools``, ``process_input_structured``,
``process_input_stream_chunks``) so ``deile/core/agent.py`` stays focused on
lifecycle, orchestration and the non-streaming ``process_input`` path. The
mixin relies on attributes/methods provided by ``DeileAgent`` (``self.X``);
it is not usable standalone.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional

from ..parsers.base import ParseResult
from ..tools.base import ToolResult, ToolStatus
from ..ui.stage_cascade import cascade_until
from ..ui.stage_messages import get_stage_message
from .exceptions import ModelError

if TYPE_CHECKING:
    from .agent import AgentSession
    from .models.stream_events import UnifiedStreamEvent


class AgentStreamingMixin:
    """Streaming-pipeline methods of :class:`DeileAgent`.

    Methods here read state from ``self`` exactly as they did when they lived
    on the concrete class — instance attributes (``self.tool_registry``,
    ``self.context_manager``, ``self._status``, ...) and sibling methods
    (``self._parse_input``, ``self._apply_validation_gate``, ...) are
    resolved via the MRO when ``DeileAgent(AgentStreamingMixin)`` is
    instantiated.
    """

    async def process_input_stream(
        self,
        user_input: str,
        session_id: str = "default",
        **kwargs,
    ) -> AsyncIterator["UnifiedStreamEvent"]:
        """Stream the same turn that ``process_input`` would produce — but as
        ``UnifiedStreamEvent`` objects so the UI can render text deltas and
        tool calls as they happen.

        Slash commands, autonomous processing, workflow paths and budget /
        configuration errors are surfaced as a single TEXT_DELTA + USAGE_FINAL
        (or a single ERROR) — they don't stream natively. The chat-with-tools
        path is the one that exhibits real progressive disclosure: the
        ``ToolLoopExecutor`` forwards every text/tool event and emits
        TOOL_RESULT after each tool invocation.
        """
        # Local imports keep agent.py import-time light and avoid pulling
        # stream_events into modules that only need the non-streaming path.
        from deile.core.models.stream_events import (ModelUsageSnapshot,
                                                     StreamEventType,
                                                     UnifiedStreamEvent)

        # Imported lazily to avoid a circular import (agent_streaming is
        # imported by agent.py, and AgentStatus lives in agent.py).
        from .agent import AgentStatus, _BudgetExceeded

        start_time = time.time()
        self._status = AgentStatus.PROCESSING
        self._request_count += 1

        try:
            session = self._get_or_create_session(session_id, **kwargs)
            session.update_activity()
            session.add_to_history("user", user_input)

            # Slash commands — non-streaming, emit aggregated text once.
            # Unknown /commands fall through to the LLM as natural language.
            _stripped_input = user_input.strip()
            _slash_cmd_name = _stripped_input[1:].split()[0] if _stripped_input.startswith('/') and _stripped_input[1:] else ""
            if _slash_cmd_name and self.command_registry.has_command(_slash_cmd_name):
                cmd_name = _slash_cmd_name
                # Map command name to a specific scenario key when one exists.
                # Strip command suffixes like "/patch-apply" → "patch_apply".
                _normalized = cmd_name.replace("-", "_").lower()
                _slash_key_map = {
                    "plan": "slash_plan",
                    "p": "slash_plan",
                    "run": "slash_run",
                    "sandbox": "slash_sandbox",
                    "memory": "slash_memory",
                    "compact": "slash_compact",
                    "patch_apply": "slash_patch_apply",
                    "patch_generate": "slash_patch_generate",
                    "apply": "slash_patch_apply",
                    "help": "slash_help",
                    "model": "slash_model_list",
                    "tools": "slash_tools",
                    "logs": "slash_logs",
                    "diff": "slash_diff",
                    "permissions": "slash_permissions",
                    "config": "slash_config",
                    "status": "slash_status",
                    "clear": "slash_clear",
                    "cls": "slash_clear",
                    "cost": "slash_cost",
                    "context": "slash_context",
                }
                _slash_key = _slash_key_map.get(_normalized, "slash_generic")
                _slash_ctx: Dict[str, Any] = (
                    {} if _slash_key != "slash_generic" else {"cmd": cmd_name}
                )
                # Cascade so the user sees label evolution if the slash work
                # blocks for >3s/10s/30s (e.g. /plan create on a heavy ticket,
                # /sandbox docker setup, /memory compact, etc.).
                _slash_response_holder: List[Any] = [None]

                async def _run_slash() -> Any:
                    return await self._process_slash_command(
                        user_input.strip(), session, start_time
                    )

                async for _item in cascade_until(
                    _run_slash(),
                    message_key=_slash_key,
                    **_slash_ctx,
                ):
                    if isinstance(_item, tuple) and _item and _item[0] == "result":
                        _slash_response_holder[0] = _item[1]
                    elif isinstance(_item, UnifiedStreamEvent):
                        yield _item
                response = _slash_response_holder[0]
                if response.content:
                    # Slash commands may return Rich renderables (Table,
                    # Panel, etc. — e.g. /model list returns a Table) OR
                    # plain text. We forward each kind through its own
                    # event type so the renderer can let Rich's
                    # width-aware layout run at the ACTUAL terminal width.
                    # Previously we flattened renderables to a fixed-width
                    # text snapshot and yielded TEXT_DELTA, which the
                    # renderer then passed through Markdown() — Markdown
                    # read the box-drawing chars as paragraphs and
                    # word-wrapped them, shattering the table layout.
                    payload = response.content
                    if isinstance(payload, str):
                        yield UnifiedStreamEvent(
                            type=StreamEventType.TEXT_DELTA, text=payload
                        )
                    else:
                        yield UnifiedStreamEvent(
                            type=StreamEventType.RICH_RENDERABLE,
                            renderable=payload,
                        )
                yield UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL, usage=ModelUsageSnapshot()
                )
                self._status = AgentStatus.IDLE
                return

            # Autonomous path — non-streaming.
            yield UnifiedStreamEvent(
                type=StreamEventType.STAGE,
                stage=get_stage_message("autonomous_process", "initial"),
            )
            autonomous_result = await self.process_autonomous_request(user_input, session)
            if autonomous_result:
                session.add_to_history(
                    "assistant",
                    autonomous_result,
                    {
                        "autonomous": True,
                        "execution_time": time.time() - start_time,
                    },
                )
                yield UnifiedStreamEvent(
                    type=StreamEventType.TEXT_DELTA, text=autonomous_result
                )
                yield UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL, usage=ModelUsageSnapshot()
                )
                self._status = AgentStatus.IDLE
                return

            yield UnifiedStreamEvent(
                type=StreamEventType.STAGE,
                stage=get_stage_message("parse_input", "initial"),
            )
            parse_result = await self._parse_input(user_input, session)

            yield UnifiedStreamEvent(
                type=StreamEventType.STAGE,
                stage=get_stage_message("proactive_tools", "initial"),
            )
            # Stream proactive tool executions so the user sees each one (name +
            # args + result) instead of just a generic stage spinner. The stream
            # yields UnifiedStreamEvents AND a final ("results", list) sentinel.
            proactive_results: List[ToolResult] = []
            async for _item in self._execute_proactive_tools_stream(user_input, session):
                if isinstance(_item, tuple) and _item and _item[0] == "results":
                    proactive_results = _item[1]
                elif isinstance(_item, UnifiedStreamEvent):
                    yield _item

            yield UnifiedStreamEvent(
                type=StreamEventType.STAGE,
                stage=get_stage_message("check_workflow", "initial"),
            )
            workflow_needed = await self._should_create_workflow(user_input, parse_result)

            if workflow_needed and self.workflow_executor:
                yield UnifiedStreamEvent(
                    type=StreamEventType.STAGE,
                    stage=get_stage_message("workflow_execute", "initial", steps="?"),
                )
                response_content, tool_results = await self._process_with_workflow(
                    user_input, parse_result, session
                )
                if response_content:
                    yield UnifiedStreamEvent(
                        type=StreamEventType.TEXT_DELTA, text=response_content
                    )
                yield UnifiedStreamEvent(
                    type=StreamEventType.USAGE_FINAL, usage=ModelUsageSnapshot()
                )
                # Persist
                _hist_meta = {
                    "tool_results": len(tool_results),
                    "parse_status": parse_result.status.value if parse_result else None,
                    "workflow": True,
                }
                session.add_to_history("assistant", response_content, _hist_meta)
                self._status = AgentStatus.IDLE
                return

            # Main path: stream chat-with-tools via ToolLoopExecutor.
            text_parts: List[str] = []
            collected_tool_results: List[ToolResult] = []

            async for event in self._stream_chat_with_tools(
                user_input, parse_result, session
            ):
                yield event
                if event.type is StreamEventType.TEXT_DELTA and event.text:
                    text_parts.append(event.text)
                elif event.type is StreamEventType.USAGE_FINAL and event.reasoning_content:
                    # Non-tool final turn: capture reasoning_content so it is included
                    # in history metadata and echoed back on the next turn.
                    session.context_data["_last_reasoning_content"] = event.reasoning_content
                elif event.type is StreamEventType.TOOL_RESULT:
                    # Preserve original ToolResult.metadata that the executor copied into
                    # event.tool_metadata. Critical for the validation gate, which inspects
                    # post_write_validation_required / post_write_validation_command set by
                    # write_file. Stream-only fields (function_name/tool_call_id/iteration)
                    # are merged on top so they always win over any legacy collisions.
                    _meta: Dict[str, Any] = dict(event.tool_metadata or {})
                    _meta["function_name"] = event.tool_name
                    _meta["tool_call_id"] = event.tool_call_id
                    _meta["iteration"] = event.iteration
                    tr = ToolResult(
                        status=ToolStatus.SUCCESS
                        if event.tool_status == "success"
                        else ToolStatus.ERROR,
                        message=event.tool_result_summary or "",
                        data=event.tool_result_data,
                        metadata=_meta,
                    )
                    collected_tool_results.append(tr)

            content = "".join(text_parts)

            # Validation gate — runs once at end. Pass only `collected_tool_results`
            # (the iterative tool-loop output) to match the non-streaming path at
            # process_input(): proactive_results must NOT influence the gate's
            # "validated" detection, otherwise a proactive bash_execute could
            # falsely satisfy the post-write-validation requirement of an
            # unrelated write_file the model issued during the streamed turn.
            # Emit STAGE so the user sees feedback during the potentially slow retry.
            # validation_check covers the cheap detection path (synchronous regex
            # + metadata inspection). When the gate triggers a retry, switch to
            # the validation_retry cascade so the user sees label evolution
            # during the LLM round-trip — issue #39 P0.
            #
            # Only emit the STAGE spinner when the gate might actually fire:
            # - tool calls present (potential unvalidated writes to check), OR
            # - short response with a promise pattern (anti-hallucination check).
            # For pure text responses (no tools, no promises) the gate exits in
            # < 1 ms — emitting the spinner is noise and can interfere with the
            # terminal's last text chunk rendering.
            _gate_might_fire = bool(collected_tool_results) or (
                len(content) <= 500
                and self._contains_promise_pattern(content)
            )
            if _gate_might_fire:
                yield UnifiedStreamEvent(
                    type=StreamEventType.STAGE,
                    stage=get_stage_message("validation_check", "initial"),
                )

            _gate_holder: List[Any] = [None]

            async def _run_gate() -> tuple:
                return await self._apply_validation_gate(
                    user_input=user_input,
                    parse_result=parse_result,
                    session=session,
                    content=content,
                    tool_results=collected_tool_results,
                )

            async for _item in cascade_until(
                _run_gate(),
                message_key="validation_retry",
            ):
                if isinstance(_item, tuple) and _item and _item[0] == "result":
                    _gate_holder[0] = _item[1]
                elif isinstance(_item, UnifiedStreamEvent):
                    yield _item
            gated_content, gated_tool_results = _gate_holder[0]
            if gated_content != content:
                # _apply_validation_gate returns the retry's standalone reply
                # (see agent.py: `return new_content, …`), not `content + addendum`.
                # `content` was already streamed to the user as TEXT_DELTA events,
                # so emitting `gated_content` alone would leave two answers on
                # screen (the original now-invalidated reply plus the retry).
                # Prepend a one-line marker — rendered inside the yellow panel
                # by the streaming renderer — telling the user this corrected
                # reply REPLACES the prior streamed response.
                marker = (
                    "A resposta anterior declarou conclusão sem rodar a validação "
                    "exigida (sintaxe / execução). Esta versão a SUBSTITUI e mostra "
                    "a validação real.\n\n"
                )
                yield UnifiedStreamEvent(
                    type=StreamEventType.TEXT_DELTA,
                    text=marker + gated_content,
                    source="validation_gate",
                )

            # Match non-streaming history_meta semantics: `tool_results` reports the
            # FULL turn count (proactive + iterative + gate retry), and
            # `proactive_results` is the per-bucket subcount.
            _history_meta: Dict[str, Any] = {
                "tool_results": len(proactive_results) + len(gated_tool_results),
                "proactive_results": len(proactive_results),
                "parse_status": parse_result.status.value if parse_result else None,
                "function_calling_enabled": True,
                "streaming": True,
            }
            _pending_rc = session.context_data.pop("_last_reasoning_content", None)
            if _pending_rc:
                _history_meta["reasoning_content"] = _pending_rc
            session.add_to_history("assistant", gated_content, _history_meta)
            self._status = AgentStatus.IDLE
            return

        except Exception as exc:
            self._status = AgentStatus.ERROR
            self.logger.error(
                f"Streaming turn failed: {exc}", exc_info=True
            )
            # Surface BudgetExceeded / FORCED_MODEL with structured metadata
            # the UI can use to render Rich panels.
            err_meta: Dict[str, Any] = {"error_type": type(exc).__name__}
            if isinstance(exc, _BudgetExceeded):
                err_meta["budget_exceeded"] = True
                err_meta["provider_id"] = getattr(exc, "provider_id", None)
                err_meta["limit_type"] = getattr(exc, "limit_type", None)
            if isinstance(exc, ModelError) and getattr(exc, "error_code", "") == "FORCED_MODEL_NOT_REGISTERED":
                err_meta["forced_model_not_registered"] = True
                err_meta["error_code"] = "FORCED_MODEL_NOT_REGISTERED"
            err_meta["message"] = str(exc)
            yield UnifiedStreamEvent(
                type=StreamEventType.ERROR,
                error_envelope=err_meta,
            )
            return

    async def _stream_chat_with_tools(
        self,
        user_input: str,
        parse_result: Optional[ParseResult],
        session: "AgentSession",
    ) -> AsyncIterator["UnifiedStreamEvent"]:
        """Stream the chat-with-tools loop for a single provider.

        Reuses ``_process_iterative_function_calling``'s setup (tier classify,
        provider selection, budget guard, message conversion) but routes the
        tool-loop through ``ToolLoopExecutor`` instead of the provider's own
        ``chat_with_tools`` (which is non-streaming).

        Cascade across providers is intentionally NOT applied on the streaming
        path: a stream interrupted mid-render can't be cleanly re-issued
        against a different provider without confusing UX. If the streaming
        provider fails, the ERROR event is forwarded; the consumer can
        re-issue the turn.
        """
        from deile.core.models.base import ModelMessage as _MM
        from deile.core.models.stream_events import (StreamEventType,
                                                     UnifiedStreamEvent)
        from deile.core.tool_loop_executor import ToolLoopExecutor
        from deile.events.event_bus import Event, EventPriority, EventType

        from .agent import (AgentStatus, _record_model_used,
                            _select_configured_model_provider)

        self._status = AgentStatus.GENERATING_RESPONSE

        yield UnifiedStreamEvent(
            type=StreamEventType.STAGE,
            stage=get_stage_message("build_context", "initial"),
        )
        context = await self.context_manager.build_context(
            user_input=user_input,
            parse_result=parse_result,
            tool_results=[],
            session=session,
        )

        # Tier classification (best-effort)
        yield UnifiedStreamEvent(
            type=StreamEventType.STAGE,
            stage=get_stage_message("analyze_intent", "initial"),
        )
        model_tier: Optional[Any] = None
        try:
            from deile.core.intent_tier_mapper import classify_tier
            intent_result = await self.intent_analyzer.analyze(
                user_input=user_input,
                parse_result=parse_result,
                session_context={},
            )
            model_tier = classify_tier(intent_result)
            session.context_data["_current_tier"] = model_tier.value
        except Exception:
            model_tier = None

        # Provider selection — honors forced/preferred/default/router.
        yield UnifiedStreamEvent(
            type=StreamEventType.STAGE,
            stage=get_stage_message("select_provider", "initial"),
        )
        model_provider, forced, _, _ = _select_configured_model_provider(
            self.model_router, session
        )
        if model_provider is None:
            model_provider = await self.model_router.select_provider(
                context=context, session=session, tier=model_tier,
            )

        # Budget guard
        yield UnifiedStreamEvent(
            type=StreamEventType.STAGE,
            stage=get_stage_message("budget_guard", "initial"),
        )
        try:
            from deile.storage.usage_repository import (BudgetExceeded,
                                                        BudgetGuard,
                                                        get_usage_repository)
            _guard: Any = getattr(self, "_budget_guard_singleton", None)
            if _guard is None:
                _yaml = Path(__file__).resolve().parents[1] / "config" / "model_providers.yaml"
                try:
                    self._budget_guard_singleton = BudgetGuard.from_yaml(
                        _yaml, get_usage_repository()
                    )
                    _guard = self._budget_guard_singleton
                except Exception:
                    self._budget_guard_singleton = False
            if _guard:
                _guard.check_all(
                    session_id=session.session_id,
                    provider_id=model_provider.provider_id,
                )
        except BudgetExceeded:
            raise

        _record_model_used(session, model_provider)

        # Build messages from context + register tools
        system_instruction = None
        raw_messages: List[Any] = []
        if isinstance(context, dict):
            system_instruction = context.get("system_instruction")
            raw_messages = context.get("messages", [])

        messages_for_provider: List[_MM] = []
        for m in raw_messages:
            if isinstance(m, _MM):
                messages_for_provider.append(m)
            elif isinstance(m, dict):
                role = str(m.get("role", "user"))
                content_raw = m.get("content", "")
                msg_metadata = m.get("metadata", {}) or {}
                messages_for_provider.append(
                    _MM(role=role, content=content_raw, metadata=msg_metadata)  # type: ignore[arg-type]
                )
        if not messages_for_provider:
            messages_for_provider = [_MM(role="user", content=user_input)]

        tools = [
            t.schema for t in self.tool_registry.list_enabled()
            if getattr(t, "schema", None) is not None
        ]

        # EventBus publisher — best-effort, non-blocking.
        async def _publish_tool_event(kind: str, name: str, **kw: Any) -> None:
            try:
                from deile.events.event_bus import \
                    get_event_bus  # type: ignore
                bus = get_event_bus()
                kind_to_event = {
                    "invoked": EventType.TOOL_INVOKED,
                    "completed": EventType.TOOL_COMPLETED,
                    "failed": EventType.TOOL_FAILED,
                }
                evt_type = kind_to_event.get(kind)
                if evt_type is None:
                    return
                event = Event(
                    event_type=evt_type,
                    source=f"agent:{model_provider.provider_id}",
                    data={"tool_name": name, **kw},
                    priority=EventPriority.LOW,
                )
                asyncio.create_task(bus.publish(event))
            except Exception:
                pass

        executor = ToolLoopExecutor(
            tool_registry=self.tool_registry,
            event_publisher=_publish_tool_event,
        )
        _provider_label = (
            getattr(model_provider, "model_name", None)
            or getattr(model_provider, "provider_id", None)
            or "model"
        )
        yield UnifiedStreamEvent(
            type=StreamEventType.STAGE,
            stage=get_stage_message("connect_model", "initial", model=_provider_label),
        )
        async for event in executor.run(
            provider=model_provider,
            messages=messages_for_provider,
            tools=tools,
            system_instruction=system_instruction,
            working_directory=str(session.working_directory),
            session_data=session.context_data,
        ):
            yield event

    async def process_input_structured(
        self,
        user_input: str,
        session_id: str = "default",
        *,
        extra_system_prompt: Any = None,
        bot_context: Any = None,
        **kwargs,
    ):
        """Bot-friendly variant: run process_input, parse output to MarkupAST."""
        from deile.core.bot_streaming import StructuredResponse, ToolCallRecord
        from deile.ui.markup import MarkdownToASTParser

        response = await self.process_input(
            user_input,
            session_id=session_id,
            extra_system_prompt=extra_system_prompt,
            bot_context=bot_context,
            **kwargs,
        )
        text = response.content or ""
        ast = MarkdownToASTParser().parse(text)
        tool_calls = []
        for tr in getattr(response, "tool_results", []) or []:
            tool_calls.append(
                ToolCallRecord(
                    name=getattr(tr, "tool_name", "") or "unknown",
                    ok=getattr(tr, "is_success", True),
                    elapsed_ms=int(getattr(tr, "execution_time", 0.0) * 1000),
                )
            )
        elapsed_ms = int(getattr(response, "execution_time", 0.0) * 1000)
        model_used = ""
        try:
            model_used = (response.metadata or {}).get("model_used", "") or ""
        except Exception:
            pass
        return StructuredResponse(
            text=text,
            markup=ast,
            tool_calls=tool_calls,
            elapsed_ms=elapsed_ms,
            model_used=model_used,
            status=getattr(response.status, "value", "idle"),
        )

    async def process_input_stream_chunks(
        self,
        user_input: str,
        session_id: str = "default",
        *,
        extra_system_prompt: Any = None,
        bot_context: Any = None,
        **kwargs,
    ):
        """Adapt UnifiedStreamEvent -> StreamChunk for bot consumers.

        Always emits `done` as the last chunk; on fatal error, emits `error` then `done`.
        """
        from deile.core.bot_streaming import StreamChunk
        from deile.ui.markup import MarkdownToASTParser

        # Stash bot params on session before streaming consumer reads them.
        session_kwargs = dict(kwargs)
        if extra_system_prompt is not None or bot_context is not None:
            session = self._get_or_create_session(session_id, **session_kwargs)
            if extra_system_prompt is not None:
                from deile.core.bot_hooks import sanitize_extra_system_prompt
                session.context_data["extra_system_prompt"] = sanitize_extra_system_prompt(
                    str(extra_system_prompt)
                )
            if bot_context is not None:
                session.context_data["bot_context"] = dict(bot_context)
            session_kwargs.pop("working_directory", None)

        accumulated_text = ""
        last_model = ""
        last_error: Any = None
        try:
            async for evt in self.process_input_stream(
                user_input, session_id=session_id, **session_kwargs
            ):
                etype = getattr(evt, "type", None)
                if etype is None:
                    continue
                name = getattr(etype, "name", None) or getattr(etype, "value", str(etype))
                if name in ("TEXT_DELTA", "text_delta"):
                    text = getattr(evt, "text", "") or ""
                    if text:
                        accumulated_text += text
                        yield StreamChunk(
                            "text", {"text": text, "incremental": True}
                        )
                elif name in ("TOOL_INVOKED", "tool_invoked"):
                    yield StreamChunk(
                        "tool_call_started",
                        {
                            "tool_name": getattr(evt, "tool_name", "") or "",
                            "args_preview": str(getattr(evt, "tool_args", ""))[:120],
                        },
                    )
                elif name in ("TOOL_RESULT", "tool_result"):
                    yield StreamChunk(
                        "tool_call_finished",
                        {
                            "tool_name": getattr(evt, "tool_name", "") or "",
                            "ok": getattr(evt, "ok", True),
                            "elapsed_ms": int(getattr(evt, "elapsed_ms", 0) or 0),
                        },
                    )
                elif name in ("USAGE_FINAL", "usage_final"):
                    usage = getattr(evt, "usage", None)
                    last_model = getattr(usage, "model", "") if usage else ""
                elif name in ("ERROR", "error"):
                    last_error = {
                        "type": getattr(evt, "error_type", "") or "Error",
                        "message": getattr(evt, "error_message", "") or "",
                    }
        except Exception as e:  # noqa: BLE001
            last_error = {"type": type(e).__name__, "message": str(e)}

        if last_error is not None:
            yield StreamChunk("error", last_error)
        ast = MarkdownToASTParser().parse(accumulated_text)
        yield StreamChunk(
            "done",
            {
                "text": accumulated_text,
                "markup": ast,
                "elapsed_ms": 0,
                "model_used": last_model,
            },
        )
