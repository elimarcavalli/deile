"""Tests para o filtro de entradas display-only no ``ContextManager`` (issue #257).

Crítico para correção do bug D1 da revisão crítica: a tool
``dispatch_parallel_subagents`` grava entrada role=assistant com metadata
``subagent_panel_summary=True`` para preservar o painel ao ``/resume``.
Sem filtro, essa entrada vai pro provider LLM (Anthropic 400 sob duas
assistants seguidas; OpenAI percepção corrompida do próprio histórico).

``ContextManager.build_context`` agora chama ``is_display_only_entry`` em
cada entrada e pula as display-only ao construir ``messages`` enviadas ao
provider. ``replay_history`` continua renderizando essas entradas.
"""
from __future__ import annotations

import pytest

from deile.orchestration.subagents.constants import (HISTORY_MARKER_KEY,
                                                     is_display_only_entry)

pytestmark = pytest.mark.unit


def test_is_display_only_entry_true_for_marked():
    assert is_display_only_entry({HISTORY_MARKER_KEY: True}) is True
    assert is_display_only_entry({HISTORY_MARKER_KEY: True, "ok_count": 3}) is True


def test_is_display_only_entry_false_for_unmarked():
    assert is_display_only_entry({}) is False
    assert is_display_only_entry({"validation_gate_pre": True}) is False
    assert is_display_only_entry({HISTORY_MARKER_KEY: False}) is False


def test_is_display_only_entry_handles_invalid_input():
    assert is_display_only_entry(None) is False  # type: ignore
    assert is_display_only_entry("string") is False  # type: ignore


async def test_build_context_filters_display_only_assistant_entries():
    """Garante que entradas com HISTORY_MARKER_KEY NÃO chegam ao provider.

    Sem este filtro, ``replay_history`` salva painel mas o LLM recebe duas
    'assistant' seguidas (própria mensagem + marker) e quebra a alternância
    exigida por Anthropic.
    """
    from unittest.mock import MagicMock

    from deile.core.context_manager import ContextManager

    cm = ContextManager.__new__(ContextManager)
    # Stubs mínimos pra build_context (não testamos as outras dependências).
    cm.persona_manager = None
    cm.embedding_store = None
    cm.deile_md_loader = MagicMock()
    cm.deile_md_loader.collect.return_value = MagicMock(rendered_block=None)
    cm.memory_manager = None
    cm.instruction_loader = MagicMock()
    cm.instruction_loader.load = MagicMock(return_value=None)
    cm._context_builds = 0
    cm._token_count = 0
    cm.max_context_tokens = 8000
    cm.cache_enabled = False

    # Sessão mock com 3 entradas: user, assistant LLM real, assistant marker.
    session = MagicMock()
    session.session_id = "test"
    session.conversation_history = [
        {"role": "user", "content": "build me X"},
        {"role": "assistant", "content": "Done.", "metadata": {}},
        {
            "role": "assistant",
            "content": "🧩 panel summary markdown",
            "metadata": {HISTORY_MARKER_KEY: True, "ok_count": 2},
        },
        {"role": "user", "content": "what next?"},
    ]

    ctx = await cm.build_context(
        user_input="what next?",
        parse_result=None,
        tool_results=[],
        session=session,
    )

    messages = ctx["messages"]
    # Marker NÃO deve aparecer no contexto enviado ao provider.
    assert all("panel summary markdown" not in str(m.get("content", "")) for m in messages)
    # As outras entradas estão lá.
    contents = [m["content"] for m in messages]
    assert "build me X" in contents
    assert "Done." in contents
    assert "what next?" in contents
    # Sequência preservada (sem o marker no meio).
    assert len(messages) == 3
