"""Testes para apply_validation_gate — fix #779 history rollback."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_session(initial_history=None):
    session = MagicMock()
    session.conversation_history = list(initial_history or [])
    session.context_data = {}

    def _add_to_history(role, content, meta=None):
        session.conversation_history.append({"role": role, "content": content})

    session.add_to_history = _add_to_history
    return session


@pytest.mark.unit
class TestValidationGateHistoryRollback:
    """apply_validation_gate faz rollback de entradas fantasma quando retry falha."""

    async def test_history_rollback_on_retry_exception(self):
        """AC-2a: Exception no retry → conversation_history inalterado."""
        from deile.core.validation_gate import apply_validation_gate
        from deile.tools.base import ToolResult, ToolStatus

        session = _make_session()
        initial_len = len(session.conversation_history)

        content = "I'll test it now"  # dispara promise gate (< 500 chars, sem tool_results)

        async def _failing_retry(*, user_input, parse_result, session):
            raise RuntimeError("provider caiu")

        result_content, result_tools = await apply_validation_gate(
            user_input="user text",
            parse_result=MagicMock(return_value=("ok", [])),
            session=session,
            content=content,
            tool_results=[],
            retry=_failing_retry,
        )

        # Deve retornar o conteúdo pré-gate, SEM entradas residuais
        assert len(session.conversation_history) == initial_len, (
            f"esperava {initial_len} entradas; history tem {len(session.conversation_history)}: "
            f"{session.conversation_history}"
        )
        assert result_content == content

    async def test_history_preserved_on_success(self):
        """AC-2b: no caminho de sucesso, o histórico mantém o comportamento atual."""
        from deile.core.validation_gate import apply_validation_gate

        session = _make_session()

        content = "I'll test it now"

        async def _ok_retry(*, user_input, parse_result, session):
            return "resultado correto", []

        result_content, _ = await apply_validation_gate(
            user_input="user text",
            parse_result=MagicMock(return_value=("ok", [])),
            session=session,
            content=content,
            tool_results=[],
            retry=_ok_retry,
        )

        assert result_content == "resultado correto"
        # Histórico deve ter as entradas do gate (não rollback no caminho de sucesso)
        assert len(session.conversation_history) >= 2
