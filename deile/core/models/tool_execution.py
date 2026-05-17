"""Shared tool-execution helper for concrete LLM providers.

The three concrete providers (``anthropic_provider``, ``openai_provider``,
``gemini_provider``) each run a tool-call loop. The middle step of that loop —
resolve a tool name against the :class:`ToolRegistry`, handle the
tool-not-found case, execute the tool and wrap any unhandled exception into a
:class:`ToolResult` — is identical logic across all three. Only the *payload*
formatting around the resulting :class:`ToolResult` is provider-specific
(Anthropic ``tool_result`` block, OpenAI ``tool`` message, Gemini
``function_response`` part), so that part stays in each provider.

Provider-agnostic: this module must NOT import any external SDK. It depends
only on ``deile.tools`` (the in-house tool layer).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def payload_to_text(payload: Any) -> str:
    """Serialize a tool-result payload to the plain-text form a chat protocol expects.

    A ``str`` payload passes through unchanged; any other object is JSON-encoded
    (``default=str`` for non-serializable leaves), falling back to ``str()`` when
    even that fails. Shared verbatim by the Anthropic ``tool_result`` block and the
    OpenAI ``role=tool`` message so the two providers cannot drift apart. Gemini is
    deliberately *not* a caller — its ``function_response`` part needs a different
    serialization (``GeminiProvider._stringify_for_model``), so do not route it here.
    """
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, default=str)
    except (TypeError, ValueError):
        return str(payload)

# Outcome markers returned alongside the ToolResult. The provider uses these to
# pick the right payload shape (e.g. a not-found / exception error vs an error
# the tool itself reported), keeping each provider's payload byte-identical to
# its pre-refactor form.
OUTCOME_NOT_FOUND = "not_found"
OUTCOME_EXCEPTION = "exception"
OUTCOME_RAN = "ran"


async def resolve_and_execute_tool(
    *,
    name: str,
    args: Dict[str, Any],
    not_found_message_fn: Callable[[str, list], str],
    context_factory: Callable[[str, Dict[str, Any], Any], Any],
    not_found_metadata: Optional[Dict[str, Any]] = None,
    exception_message_fn: Callable[[str, Exception], str] = lambda n, exc: str(exc),
    exception_metadata: Optional[Dict[str, Any]] = None,
    log_calls: bool = False,
) -> Tuple[Any, str]:
    """Resolve ``name`` via the ToolRegistry and run it.

    Returns a ``(ToolResult, outcome)`` tuple where ``outcome`` is one of
    :data:`OUTCOME_NOT_FOUND`, :data:`OUTCOME_EXCEPTION` or :data:`OUTCOME_RAN`:

    * **tool not found** — an ``ERROR`` ToolResult whose ``message`` is built by
      ``not_found_message_fn(name, available_tool_names)`` and whose ``metadata``
      is a fresh shallow copy of ``not_found_metadata`` (or ``{}`` when none is
      given); outcome :data:`OUTCOME_NOT_FOUND`.
    * **tool raised** — an ``ERROR`` ToolResult carrying the exception, with
      ``message`` built by ``exception_message_fn(name, exc)`` and ``metadata``
      seeded from ``exception_metadata`` (or ``{}`` when none is given); outcome
      :data:`OUTCOME_EXCEPTION`.
    * **tool ran** — the ToolResult returned by the tool itself, unchanged;
      outcome :data:`OUTCOME_RAN`.

    The provider is responsible for turning that ToolResult into its own
    payload shape afterwards.

    Args:
        name: tool name requested by the model.
        args: raw arguments for the tool.
        not_found_message_fn: builds the not-found error message from
            ``(name, sorted_available_names)``.
        context_factory: builds the :class:`~deile.tools.base.ToolContext`
            from ``(name, args, tool)`` — providers differ in which context
            fields they populate; the resolved tool is passed so a provider
            can stamp tool-specific data (e.g. the canonical ``tool.name``).
        not_found_metadata: metadata dict for the not-found ToolResult.
        exception_message_fn: builds the message for an unhandled tool
            exception; defaults to ``str(exc)``.
        exception_metadata: metadata dict for the exception ToolResult.
        log_calls: when ``True``, emit info/warning/error logs around
            resolution and execution (Gemini's historical behaviour).
    """
    from deile.tools.base import ToolResult, ToolStatus
    from deile.tools.registry import get_tool_registry

    registry = get_tool_registry()
    tool = registry.get(name) if hasattr(registry, "get") else None

    if tool is None:
        available = (
            sorted(registry._tools.keys()) if hasattr(registry, "_tools") else []
        )
        message = not_found_message_fn(name, available)
        if log_calls:
            logger.warning(
                "Function call '%s' not found in registry. Available tools: %s",
                name,
                available,
            )
        return (
            ToolResult(
                status=ToolStatus.ERROR,
                message=message,
                metadata=dict(not_found_metadata) if not_found_metadata else {},
            ),
            OUTCOME_NOT_FOUND,
        )

    ctx = context_factory(name, args, tool)
    if log_calls:
        logger.info(
            "Executing function call '%s' with args=%s (cwd=%s)",
            name,
            list(getattr(ctx, "parsed_args", {}) or {}),
            getattr(ctx, "working_directory", "."),
        )

    try:
        return await tool.execute(ctx), OUTCOME_RAN
    except Exception as exc:  # pylint: disable=broad-except
        if log_calls:
            logger.error(
                "Tool '%s' raised an unhandled exception: %s",
                name,
                exc,
                exc_info=True,
            )
        return (
            ToolResult(
                status=ToolStatus.ERROR,
                message=exception_message_fn(name, exc),
                error=exc,
                metadata=dict(exception_metadata) if exception_metadata else {},
            ),
            OUTCOME_EXCEPTION,
        )
