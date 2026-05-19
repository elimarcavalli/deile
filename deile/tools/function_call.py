"""Ponte síncrona de Function Calling para Tools.

Extraído de :class:`~deile.tools.registry.ToolRegistry` (SRP): executar
uma function-call de forma síncrona — incluindo a ponte coroutine→sync
para tools async — é uma responsabilidade de I/O distinta do registro e
da descoberta de tools. O registry expõe ``execute_function_call()`` que
delega para estas funções, no mesmo padrão de ``schema_export.py`` e
``schema_validation.py``.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Dict, Optional

from .base import ToolContext, ToolResult
from .schema_validation import validate_function_arguments

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = logging.getLogger(__name__)


def run_coro_sync(coro):
    """Run a coroutine to completion from synchronous code.

    Uses ``asyncio.run`` when no loop is active. When invoked from inside
    a running loop (e.g. ``PlanManager._run_tool_with_params``), the
    coroutine is run in a worker thread so it never reenters the live
    loop — ``loop.run_until_complete`` would raise ``RuntimeError`` there.

    Two constraints apply when the worker-thread path is taken, and
    callers must account for both:

    1. Cancellation/timeout does NOT cross into the worker thread. An
       ``asyncio.CancelledError`` or timeout raised against the caller
       (e.g. an ``asyncio.wait_for`` wrapping a ``PlanManager`` step)
       cannot interrupt the blocking ``.result()`` call — the worker
       runs the coroutine to completion regardless. Step-level
       timeout/cancellation therefore does not propagate into the tool.
    2. The coroutine runs on a fresh event loop in a different thread.
       Any tool invoked through this sync bridge must NOT hold or
       capture resources bound to the caller's event loop (e.g.
       ``asyncio.Lock``, connection pools, async clients/sessions);
       such resources will misbehave or raise
       ``RuntimeError: ... attached to a different loop``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def execute_function_call(
    registry: "ToolRegistry",
    function_name: str,
    arguments: Dict[str, Any],
    execution_context: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    """Executa uma function call de forma síncrona.

    Function Calling é síncrono na API dos providers; tools async são
    executadas via :func:`run_coro_sync`. Retorna sempre um ``ToolResult``
    — falhas de resolução, validação ou execução são mapeadas para
    ``ToolResult.error_result``.
    """
    tool = registry.get_enabled(function_name)
    if tool is None:
        return ToolResult.error_result(
            f"Function '{function_name}' not found or disabled",
            error_code="FUNCTION_NOT_FOUND",
        )

    if tool.schema:
        validation_result = validate_function_arguments(tool.schema, arguments)
        if not validation_result["valid"]:
            return ToolResult.error_result(
                f"Invalid arguments for '{function_name}': "
                f"{validation_result['errors']}",
                error_code="INVALID_ARGUMENTS",
            )

    context = ToolContext(
        user_input="",  # Function calls não têm user_input direto
        parsed_args=arguments,
        session_data=execution_context or {},
        working_directory=(
            execution_context.get("working_directory", ".")
            if execution_context
            else "."
        ),
        metadata={
            "execution_method": "function_call",
            "function_name": function_name,
            "tool_name": tool.name,
        },
    )

    try:
        # Se é SyncTool, executa diretamente.
        if hasattr(tool, "execute_sync"):
            return tool.execute_sync(context)
        # Executa async tool de forma síncrona — seguro tanto fora
        # quanto dentro de um event loop ativo.
        return run_coro_sync(tool.execute(context))
    except Exception as e:
        logger.error(f"Error executing function call '{function_name}': {e}")
        return ToolResult.error_result(
            f"Execution error: {str(e)}",
            error=e,
            error_code="EXECUTION_ERROR",
        )
