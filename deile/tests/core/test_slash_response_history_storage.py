"""Verifica que respostas de slash commands são armazenadas como string no histórico.

Cobre o invariante: session.conversation_history deve ser sempre JSON-serializável,
independentemente do tipo de content_type que o CommandResult retornar.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from rich.panel import Panel

from deile.commands.base import CommandResult
from deile.core.agent import AgentResponse, AgentSession, AgentStatus, DeileAgent


def _make_agent() -> DeileAgent:
    agent = DeileAgent.__new__(DeileAgent)
    agent.config_manager = None
    agent.command_registry = MagicMock()
    agent.logger = MagicMock()
    agent.settings = MagicMock()
    return agent


def _make_session() -> AgentSession:
    return AgentSession(session_id="test-slash-history", working_directory=Path("/tmp"))


def _make_rich_command_result(panel: Panel) -> CommandResult:
    return CommandResult.success_result(panel, content_type="rich")


def _make_string_command_result(text: str) -> CommandResult:
    return CommandResult.success_result(text, content_type="text")


@pytest.mark.unit
async def test_rich_panel_response_stored_as_string_in_history():
    agent = _make_agent()
    session = _make_session()

    panel = Panel("Conteúdo do painel de ajuda")
    agent.command_registry.execute_command = AsyncMock(
        return_value=_make_rich_command_result(panel)
    )

    await agent._process_slash_command("/help", session, time.time())

    assert len(session.conversation_history) == 1
    stored = session.conversation_history[0]["content"]
    assert isinstance(stored, str), f"Esperado str, obtido {type(stored)}"
    assert stored  # não vazio


@pytest.mark.unit
async def test_string_response_stored_unchanged():
    agent = _make_agent()
    session = _make_session()

    agent.command_registry.execute_command = AsyncMock(
        return_value=_make_string_command_result("Resposta de texto simples")
    )

    await agent._process_slash_command("/version", session, time.time())

    assert len(session.conversation_history) == 1
    stored = session.conversation_history[0]["content"]
    assert stored == "Resposta de texto simples"


@pytest.mark.unit
async def test_history_json_serializable_after_rich_command():
    agent = _make_agent()
    session = _make_session()

    panel = Panel("[bold]Ajuda[/bold]", title="DEILE Help")
    agent.command_registry.execute_command = AsyncMock(
        return_value=_make_rich_command_result(panel)
    )

    await agent._process_slash_command("/help", session, time.time())

    try:
        json.dumps(session.conversation_history)
    except TypeError as exc:
        pytest.fail(f"conversation_history não é JSON-serializável: {exc}")
