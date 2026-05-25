"""Constantes compartilhadas entre a tool, o orchestrator e a CLI (issue #257).

Mantidas em módulo separado para evitar acoplamento direto entre camadas:

  * ``deile/tools/dispatch_parallel_subagents.py`` escreve a entrada de
    histórico marcada;
  * ``deile/cli_session_helpers.py`` (``replay_history``) lê a marca para
    re-renderizar o painel no ``/resume``;
  * ``deile/core/context_manager.py`` (``build_context``) **filtra** essas
    entradas — elas são display-only e não devem ir para o provider LLM
    (Anthropic 400 em duas assistants seguidas; OpenAI percepção corrompida).
"""

from __future__ import annotations

# Metadata flag em entradas de :attr:`AgentSession.conversation_history` que
# representam o resumo final de uma chamada de ``dispatch_parallel_subagents``.
# Sobrevive ao roundtrip JSON do ``SessionHistoryStore`` para preservar o
# painel ao ``/resume``.
HISTORY_MARKER_KEY: str = "subagent_panel_summary"


def is_display_only_entry(metadata: dict) -> bool:
    """True se a entrada deve ser pulada pelo ``ContextManager.build_context``.

    Hoje apenas o marker de painel de sub-DEILEs sinaliza display-only. Se
    outros tipos surgirem (ex: marcadores de UI futuros), basta adicionar
    aqui — único ponto de verdade.
    """
    if not isinstance(metadata, dict):
        return False
    return bool(metadata.get(HISTORY_MARKER_KEY))


__all__ = ["HISTORY_MARKER_KEY", "is_display_only_entry"]
