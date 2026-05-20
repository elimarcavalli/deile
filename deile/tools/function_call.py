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
import re
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

# Re-entrancy detection: per-thread flag, set in the caller (NOT in the worker)
# while a bridge submit is in flight. If the same thread re-enters
# ``_run_coro_sync`` while already mid-bridge — i.e. a tool invoked via the
# bridge transitively triggered another ``execute_function_call`` — we raise
# instead of submitting and deadlocking on the single-worker executor.
_BRIDGE_ACTIVE = threading.local()

# Control-character/ANSI-escape stripper for values that flow into log records
# (e.g. ``function_name`` originates from the LLM, ``str(exc)`` may contain
# arbitrary content). Removes C0/C1 controls so a malicious value cannot inject
# fake log lines via CR/LF or hide tracks with ANSI cursor controls.
_LOG_SANITIZE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _sanitize_for_log(value: object, limit: int = 200) -> str:
    """Strip control chars from ``value`` and truncate for safe logging."""
    text = _LOG_SANITIZE_RE.sub("?", str(value))
    if len(text) > limit:
        text = text[:limit] + "..."
    return text


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
    # Treat a shut-down executor identically to "not yet created": a late
    # call arriving after an external ``atexit`` (or test teardown) ran would
    # otherwise hit ``submit() -> RuntimeError`` and leak the unawaited
    # coroutine. Re-instantiate lazily instead.
    if _BRIDGE_EXECUTOR is None or getattr(_BRIDGE_EXECUTOR, "_shutdown", False):
        with _BRIDGE_LOCK:
            if _BRIDGE_EXECUTOR is None or getattr(
                _BRIDGE_EXECUTOR, "_shutdown", False
            ):
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
       transitiva no bridge a partir do worker é proibida. Esta invariante
       é enforced em runtime via ``_BRIDGE_ACTIVE`` (threading.local) — a
       reentrada levanta ``RuntimeError`` antes do ``submit()`` em vez de
       deadlockar.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Re-entrancy guard: if this thread is already mid-bridge, submitting
    # would deadlock on the single-worker pool (constraint 3 above). Detect
    # and raise instead.
    if getattr(_BRIDGE_ACTIVE, "in_call", False):
        coro.close()
        raise RuntimeError(
            "re-entrant bridge call: _run_coro_sync invoked transitively "
            "from a coroutine already being bridged (would deadlock the "
            "single-worker executor)"
        )
    _BRIDGE_ACTIVE.in_call = True
    try:
        try:
            future = _get_bridge_executor().submit(asyncio.run, coro)
        except RuntimeError:
            # Executor was shut down between the lazy check and submit() —
            # close the coroutine to avoid the "coroutine was never awaited"
            # warning and re-raise so the caller sees the lifecycle issue.
            coro.close()
            raise
        return future.result()
    finally:
        _BRIDGE_ACTIVE.in_call = False


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

    ec = execution_context or {}
    context = ToolContext(
        user_input="",  # Function calls não têm user_input direto
        parsed_args=arguments,
        session_data=ec,
        working_directory=ec.get("working_directory", "."),
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
        # ``function_name`` is LLM-controlled and ``str(e)`` may carry
        # untrusted/secret content — sanitize both before they reach the
        # log record. ``exc_info=True`` captures the traceback through the
        # logger's own handlers (no string interpolation of the exception).
        logger.error(
            "Error executing function call '%s' (%s)",
            _sanitize_for_log(function_name),
            type(e).__name__,
            exc_info=True,
        )
        return ToolResult.error_result(
            f"Execution error: {type(e).__name__}",
            # error= preserva exceção crua p/ introspecção; surfaces NÃO
            # devem str(result.error) — usar result.message
            error=e,
            error_code="EXECUTION_ERROR",
        )
