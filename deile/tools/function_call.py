"""Ponte síncrona de Function Calling para Tools.

Extraído de :class:`~deile.tools.registry.ToolRegistry` (SRP): executar
uma function-call de forma síncrona — incluindo a ponte coroutine→sync
para tools async — é uma responsabilidade de I/O distinta do registro e
da descoberta de tools. O registry expõe ``execute_function_call()`` que
delega para estas funções, no mesmo padrão de ``schema_export.py`` e
``schema_validation.py``.

O helper de bridging síncrono :func:`_run_coro_sync` é privado ao módulo
— callers externos devem usar :func:`execute_function_call`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Dict, Optional

from .base import ToolContext, ToolResult
from .schema_validation import validate_function_arguments

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = logging.getLogger(__name__)


# Executor module-level reusado entre chamadas a :func:`_run_coro_sync` quando
# há event loop ativo. Cada submit ainda executa ``asyncio.run(coro)``, então
# a semântica de "loop fresco por chamada" é preservada — apenas a thread é
# reaproveitada, eliminando o overhead de spawn/teardown por call.
_BRIDGE_EXECUTOR: Optional[ThreadPoolExecutor] = None
_BRIDGE_LOCK = threading.Lock()


def _get_bridge_executor() -> ThreadPoolExecutor:
    """Retorna o ``ThreadPoolExecutor`` único usado pela ponte sync→async.

    Lazy-init com double-checked locking. ``max_workers=1`` é intencional:
    a ponte serializa coroutines (uma por vez) — paralelismo deve usar a
    própria API async, não esta ponte.

    Decisão sobre ``daemon`` da worker thread: **não marcamos** a thread
    como daemon. ``ThreadPoolExecutor`` invoca o ``initializer`` dentro
    de ``_worker()`` *depois* de ``Thread.start()``; mutar
    ``Thread.daemon`` em thread já ativa levanta
    ``RuntimeError: cannot set daemon status of active thread`` e marca
    o pool como ``_broken`` — toda chamada subsequente lança
    ``BrokenThreadPool``. A alternativa de subclassar o executor e
    sobrescrever ``_adjust_thread_count`` para criar a Thread com
    ``daemon=True`` antes do ``start()`` acopla este módulo a uma API
    privada de ``concurrent.futures.thread``. O ``atexit`` handler
    ``concurrent.futures.thread._python_exit`` já dá join nos workers
    no shutdown normal do interpretador; sob ``SIGTERM`` qualquer
    coroutine em vôo completa antes do exit — comportamento desejável
    para evitar perda de trabalho de tool em execução.
    """
    global _BRIDGE_EXECUTOR
    if _BRIDGE_EXECUTOR is None:
        with _BRIDGE_LOCK:
            if _BRIDGE_EXECUTOR is None:
                _BRIDGE_EXECUTOR = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="deile-sync-bridge",
                )
    return _BRIDGE_EXECUTOR


def _run_coro_sync(coro):
    """Run a coroutine to completion from synchronous code.

    Uses ``asyncio.run`` when no loop is active. When invoked from inside
    a running loop (e.g. ``PlanManager._run_tool_with_params``), the
    coroutine is run in a worker thread so it never reenters the live
    loop — ``loop.run_until_complete`` would raise ``RuntimeError`` there.

    Three constraints apply when the worker-thread path is taken, and
    callers must account for all of them:

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
    3. Re-entrancy is unsupported. Tools (sync OR async) invocadas
       via este bridge NÃO devem chamar ``execute_function_call``
       recursivamente, direta ou transitivamente — isso causa deadlock
       no executor single-worker. O módulo-level executor tem
       ``max_workers=1``, então chamar ``_run_coro_sync`` de dentro de
       uma coroutine que já está sendo bridged (via
       ``execute_function_call`` → ``tool.execute`` → ... →
       ``_run_coro_sync``) trava: o worker está ocupado com a tarefa
       externa e não consegue drenar o ``submit()`` interno. Aplica-se
       também ao caminho ``SyncTool.execute_sync`` que chama
       ``execute_function_call`` indiretamente — qualquer reentrada
       transitiva no bridge a partir do worker é proibida.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    return _get_bridge_executor().submit(asyncio.run, coro).result()


def execute_function_call(
    registry: "ToolRegistry",
    function_name: str,
    arguments: Dict[str, Any],
    execution_context: Optional[Dict[str, Any]] = None,
) -> ToolResult:
    """Executa uma function call de forma síncrona.

    Function Calling é síncrono na API dos providers; tools async são
    executadas via :func:`_run_coro_sync`. Retorna sempre um ``ToolResult``
    — falhas de resolução, validação ou execução são mapeadas para
    ``ToolResult.error_result``.

    A distinção entre ``FUNCTION_NOT_FOUND`` (tool inexistente) e
    ``FUNCTION_DISABLED`` (tool registrada mas desabilitada) é preservada
    com lookup explícito — ``registry.get_enabled`` sozinho colapsaria
    os dois casos no mesmo ``None``.
    """
    tool = registry.get(function_name)
    if tool is None:
        return ToolResult.error_result(
            f"Function '{function_name}' not found",
            error_code="FUNCTION_NOT_FOUND",
        )
    if registry.get_enabled(tool.name) is None:
        return ToolResult.error_result(
            f"Function '{function_name}' is disabled",
            error_code="FUNCTION_DISABLED",
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
        return _run_coro_sync(tool.execute(context))
    except Exception as e:
        logger.error(f"Error executing function call '{function_name}': {e}")
        return ToolResult.error_result(
            f"Execution error: {type(e).__name__}",
            # error= preserva exceção crua p/ introspecção; surfaces NÃO
            # devem str(result.error) — usar result.message
            error=e,
            error_code="EXECUTION_ERROR",
        )
