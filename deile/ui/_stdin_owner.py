"""Coordenação de exclusão para stdin em modo cbreak (issue #257 round 2).

O CLI principal (``deile/cli.py::_stream_with_esc_cancel``) usa cbreak +
``sys.stdin.read(1)`` num thread daemon para detectar ESC e cancelar o turno.
A tool ``dispatch_parallel_subagents`` abre um painel multipanel que TAMBÉM
quer ler stdin (números pra foco, ESC pra sair). Sem coordenação:

  * Ambos os watchers competem pelo mesmo byte — read() é exclusiva, o byte
    vai pra UM thread, perdido pro outro;
  * O CLI consome bytes não-ESC e os DESCARTA (não há "unread") — então
    metade das teclas do usuário no painel são perdidas.

Solução: módulo-global :class:`threading.Event` que o CLI checa antes de cada
``read(1)``. Quando o painel abre, seta o event; o CLI dorme em vez de ler,
deixando todos os bytes para o watcher do painel. Quando o painel fecha,
limpa o event.

Mantemos a abstração mínima — só uma flag — para não acoplar UI a uma classe
de coordenação genérica. A presença do flag é a única dependência implícita
entre os dois componentes; documentada aqui é melhor do que descobrir num bug.
"""

from __future__ import annotations

import threading

# Único event compartilhado. Quando set(), watchers SECUNDÁRIOS (o do CLI)
# devem pausar a leitura de stdin. O dono ATIVO (painel) é quem lê.
_panel_owns_stdin: threading.Event = threading.Event()


def claim_stdin_for_panel() -> None:
    """Painel anuncia que vai ler stdin com exclusividade."""
    _panel_owns_stdin.set()


def release_stdin_for_panel() -> None:
    """Painel devolve stdin pro CLI."""
    _panel_owns_stdin.clear()


def panel_owns_stdin() -> bool:
    """CLI consulta antes de cada read pra saber se deve pausar."""
    return _panel_owns_stdin.is_set()


__all__ = [
    "claim_stdin_for_panel",
    "panel_owns_stdin",
    "release_stdin_for_panel",
]
