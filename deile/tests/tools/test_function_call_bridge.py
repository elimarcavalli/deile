"""Unit tests for ``deile/tools/function_call``.

Pin the contract of the extracted sync function-call bridge:

* ``FUNCTION_NOT_FOUND`` vs ``FUNCTION_DISABLED`` remain distinct.
* Alias lookup resolves correctly even when alias differs from real name.
* ``_run_coro_sync`` works both outside and inside a running event loop.
"""

from __future__ import annotations

import asyncio

from deile.tools.base import SyncTool, Tool, ToolContext, ToolResult, ToolStatus
from deile.tools.function_call import _run_coro_sync, execute_function_call
from deile.tools.registry import ToolRegistry


class _AsyncEchoTool(Tool):
    """Async Tool that echoes a fixed message."""

    @property
    def name(self) -> str:
        return "echo_async"

    @property
    def description(self) -> str:
        return "echo async"

    @property
    def category(self) -> str:
        return "other"

    async def execute(self, context: ToolContext) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, data="async-ok", message="ok")


class _SyncEchoTool(SyncTool):
    """Sync Tool — exercises the ``execute_sync`` branch."""

    @property
    def name(self) -> str:
        return "echo_sync"

    @property
    def description(self) -> str:
        return "echo sync"

    @property
    def category(self) -> str:
        return "other"

    def execute_sync(self, context: ToolContext) -> ToolResult:
        return ToolResult(status=ToolStatus.SUCCESS, data="sync-ok", message="ok")


def test_unknown_function_returns_function_not_found():
    registry = ToolRegistry()

    result = execute_function_call(registry, "does_not_exist", {})

    assert result.is_error
    assert result.metadata["error_code"] == "FUNCTION_NOT_FOUND"


def test_disabled_function_returns_function_disabled():
    registry = ToolRegistry()
    tool = _AsyncEchoTool()
    registry.register(tool)
    registry.disable_tool(tool.name)

    result = execute_function_call(registry, tool.name, {})

    assert result.is_error
    assert result.metadata["error_code"] == "FUNCTION_DISABLED"


def test_alias_lookup_resolves_for_enabled_tool():
    """Regression: ``get_enabled`` previously checked alias against
    ``_enabled_tools`` (which holds real names), so calls via alias would
    falsely return ``None``. This test pins the corrected behavior."""
    registry = ToolRegistry()
    tool = _AsyncEchoTool()
    registry.register(tool, aliases=["echo_alias"])

    result = execute_function_call(registry, "echo_alias", {})

    assert result.is_success
    assert result.data == "async-ok"


def test_alias_lookup_for_disabled_tool_returns_function_disabled():
    """Pin o caminho dual: o alias resolve para a tool real, e a tool
    estando desabilitada retorna ``FUNCTION_DISABLED`` (não
    ``FUNCTION_NOT_FOUND``). Garante que disable opera sobre o nome
    real mesmo quando a invocação chega via alias."""
    registry = ToolRegistry()
    tool = _AsyncEchoTool()
    registry.register(tool, aliases=["echo_alias"])
    registry.disable_tool(tool.name)

    result = execute_function_call(registry, "echo_alias", {})

    assert result.is_error
    assert result.metadata["error_code"] == "FUNCTION_DISABLED"


def test_sync_tool_uses_execute_sync_branch():
    registry = ToolRegistry()
    tool = _SyncEchoTool()
    registry.register(tool)

    result = execute_function_call(registry, tool.name, {})

    assert result.is_success
    assert result.data == "sync-ok"


def test_run_coro_sync_outside_event_loop():
    async def coro():
        return 42

    assert _run_coro_sync(coro()) == 42


def test_run_coro_sync_inside_event_loop():
    """When called from inside a running loop, the bridge offloads to a
    worker thread — this is the path exercised by ``PlanManager``.

    Crítico: ``_run_coro_sync`` precisa ser chamado de dentro de uma
    coroutine cujo loop está ATIVO no mesmo thread — exatamente o que
    ``PlanManager._run_tool_with_params`` faz. Usar
    ``await asyncio.to_thread(_run_coro_sync, ...)`` NÃO exercita esse
    caminho: o callable roda em uma thread sem loop e cai no branch
    ``asyncio.run(coro)``, deixando o bridge intacto. Por isso aqui
    construímos um loop manual e rodamos ``run_until_complete`` para
    que ``asyncio.get_running_loop()`` dentro de ``_run_coro_sync``
    encontre um loop e force a ponte para o ``_BRIDGE_EXECUTOR``.
    """
    from deile.tools import function_call as fc_mod

    async def coro():
        return "from-bridge"

    async def caller():
        # Chamada DIRETA — sem to_thread — para garantir que estamos no
        # mesmo thread do loop ativo, hitting o branch do worker-pool.
        return _run_coro_sync(coro())

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(caller())
    finally:
        loop.close()
        asyncio.set_event_loop(None)

    assert result == "from-bridge"
    # Pin: o caminho do bridge foi efetivamente exercitado — o executor
    # módulo-level precisa ter sido materializado pela chamada acima.
    assert fc_mod._BRIDGE_EXECUTOR is not None
