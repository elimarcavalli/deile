"""Testes do comando /context — dados reais e flag --export"""

import json
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from deile.commands.base import CommandContext
from deile.commands.builtin.context_command import ContextCommand

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(
    args: str = "",
    session_id: str = "test-session",
    history: Optional[List[Dict[str, Any]]] = None,
    agent=None,
    session=None,
) -> CommandContext:
    if session is None:
        session = MagicMock()
        session.session_id = session_id
        session.conversation_history = history or []
        session.created_at = 1700000000.0
        session.last_activity = 1700000100.0
    ctx = CommandContext(user_input=f"/context {args}", args=args)
    ctx.agent = agent
    ctx.session = session
    return ctx


def _make_agent(
    tools: int = 5,
    enabled: int = 4,
    persona_name: str = "developer",
    memory_usage: Optional[Dict] = None,
) -> MagicMock:
    agent = MagicMock()

    tool_mocks = [MagicMock(category="file") for _ in range(tools)]
    enabled_mocks = tool_mocks[:enabled]
    agent.tool_registry.list_all.return_value = tool_mocks
    agent.tool_registry.list_enabled.return_value = enabled_mocks

    persona = MagicMock()
    persona.name = persona_name
    agent.persona_manager.get_active_persona.return_value = persona

    default_mem = {"total_memory_mb": 12.5, "components": {}}
    agent.memory_manager.get_memory_usage = AsyncMock(return_value=memory_usage or default_mem)

    agent.model_router.providers = {"openai": MagicMock()}
    agent.model_router.strategy = MagicMock()
    agent.context_manager.get_stats = AsyncMock(return_value={
        "context_builds": 3,
        "max_context_tokens": 200000,
        "chat_session_mode": True,
        "simplified": True,
    })

    return agent


def _render_rich(obj) -> str:
    """Render a Rich renderable to plain text."""
    from rich.console import Console
    buf = StringIO()
    console = Console(file=buf, highlight=False, markup=False, width=200)
    console.print(obj)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_summary_shows_real_persona_name():
    agent = _make_agent(persona_name="architect")
    ctx = _make_context(agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success
    rendered = _render_rich(result.content)
    assert "architect" in rendered


@pytest.mark.unit
async def test_summary_shows_real_message_count():
    history = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    agent = _make_agent()
    ctx = _make_context(args="", history=history, agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success
    rendered = _render_rich(result.content)
    assert "2" in rendered


@pytest.mark.unit
async def test_summary_shows_real_tool_counts():
    agent = _make_agent(tools=8, enabled=6)
    ctx = _make_context(agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success
    rendered = _render_rich(result.content)
    assert "6" in rendered
    assert "8" in rendered


@pytest.mark.unit
async def test_json_format_no_hardcoded_values():
    history = [{"role": "user", "content": "msg"}]
    agent = _make_agent(persona_name="custom-persona", tools=3, enabled=2)
    ctx = _make_context(args="json", history=history, agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success
    data = json.loads(result.content)
    assert data["persona"]["name"] == "custom-persona"
    assert data["tools"]["total"] == 3
    assert data["tools"]["enabled"] == 2
    assert data["conversation_history"]["messages"] == 1


@pytest.mark.unit
async def test_no_hardcoded_numbers_in_summary():
    agent = _make_agent()
    ctx = _make_context(args="json", agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    data_str = result.content
    hardcoded = [
        "2500", "8500", "16225", "session_20250906_184500",
        "gemini-2.5-pro", "Developer Assistant",
    ]
    for val in hardcoded:
        assert val not in data_str, f"Valor hardcoded encontrado: {val}"


@pytest.mark.unit
async def test_subsystem_unavailable_shows_indisponivel():
    agent = MagicMock()
    agent.tool_registry.list_all.side_effect = RuntimeError("not initialized")
    agent.tool_registry.list_enabled.side_effect = RuntimeError("not initialized")
    agent.persona_manager.get_active_persona.side_effect = RuntimeError("pm down")
    agent.memory_manager.get_memory_usage = AsyncMock(side_effect=RuntimeError("mm down"))
    agent.model_router.providers = {}
    agent.context_manager.get_stats = AsyncMock(side_effect=RuntimeError("cm down"))

    ctx = _make_context(args="json", agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success
    data = json.loads(result.content)
    assert data["tools"]["status"] == "indisponível"
    assert data["persona"]["status"] == "indisponível"
    assert data["memory"]["status"] == "indisponível"
    assert data["system_instructions"]["status"] == "indisponível"


@pytest.mark.unit
async def test_no_agent_gracefully_returns_indisponivel():
    ctx = _make_context(agent=None, session=None)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success
    # JSON format reveals indisponível values without Rich rendering
    ctx2 = _make_context(args="json", agent=None, session=None)
    result2 = await cmd.execute(ctx2)
    assert result2.success
    data = json.loads(result2.content)
    assert data["tools"].get("status") == "indisponível" or data["tools"].get("total") is None


@pytest.mark.unit
async def test_export_json_writes_valid_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _make_agent()
    ctx = _make_context(args="--export json", agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success
    exported = result.metadata.get("exported_file", "")
    assert exported.endswith(".json")
    content = json.loads(Path(exported).read_text())
    assert "conversation_history" in content


@pytest.mark.unit
async def test_export_md_writes_valid_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _make_agent()
    ctx = _make_context(args="--export md", agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success
    exported = result.metadata.get("exported_file", "")
    assert exported.endswith(".md")
    md = Path(exported).read_text()
    assert "# Exportação de Contexto DEILE" in md


@pytest.mark.unit
async def test_token_count_method_declared():
    agent = _make_agent()
    ctx = _make_context(args="json", agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    data = json.loads(result.content)
    instr = data.get("system_instructions", {})
    assert "token_count_method" in instr or instr.get("status") == "indisponível"


@pytest.mark.unit
async def test_detailed_format_no_crash():
    agent = _make_agent()
    ctx = _make_context(args="detailed", agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert result.success


@pytest.mark.unit
async def test_invalid_export_format_returns_error():
    agent = _make_agent()
    ctx = _make_context(args="--export xml", agent=agent)
    cmd = ContextCommand()
    result = await cmd.execute(ctx)
    assert not result.success
