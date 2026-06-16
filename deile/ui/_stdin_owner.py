"""Coordenação de exclusão para stdin em modo cbreak + recuperação de termios.

Issue #257 round 2 + round 3.

Round 2 (exclusão): o CLI principal usa cbreak + ``sys.stdin.read(1)`` num
thread daemon para detectar ESC. A tool ``dispatch_parallel_subagents`` abre
um painel que TAMBÉM lê stdin. Sem coordenação ambos competem pelos mesmos
bytes (read é exclusiva) e o CLI descarta bytes não-ESC. Solução: event
:func:`claim_stdin_for_panel`/:func:`release_stdin_for_panel` que o CLI
respeita pausando a leitura.

Round 3 (recuperação): o watcher do painel roda em **daemon thread**. Se o
processo morrer abruptamente (Ctrl+C, exceção não tratada, ``os._exit``), o
thread é KILLED sem rodar ``finally`` → ``termios.tcsetattr(saved)`` não
corre → o terminal fica em cbreak (digitação sem echo, Enter não funciona).
Solução: capturamos os atributos termios ORIGINAIS ANTES de qualquer setup
e registramos um ``atexit`` handler que restaura. Funciona mesmo quando
threads daemons são killed — atexit roda no main thread.
"""

from __future__ import annotations

import atexit
import logging
import sys
import threading

logger = logging.getLogger(__name__)

# Único event compartilhado. Quando set(), watchers SECUNDÁRIOS (o do CLI)
# devem pausar a leitura de stdin. O dono ATIVO (painel) é quem lê.
_panel_owns_stdin: threading.Event = threading.Event()

# Snapshot dos termios ORIGINAIS do stdin, capturados na primeira chamada
# de :func:`claim_stdin_for_panel`. ``atexit`` restaura isto se o processo
# sair em qualquer caminho (clean exit, SystemExit, exceção não tratada).
_saved_termios = None
_termios_fd: int = -1
_atexit_registered: bool = False
_lock = threading.Lock()


def _restore_termios() -> None:
    """Restaura os atributos termios capturados em ``claim_stdin_for_panel``.

    Idempotente. Tolera a ausência do módulo ``termios`` (Windows) e fd
    fechado. Não levanta — chamada por ``atexit`` que NÃO deve falhar.
    """
    global _saved_termios, _termios_fd
    if _saved_termios is None:
        return
    try:
        import termios

        if _termios_fd >= 0:
            termios.tcsetattr(_termios_fd, termios.TCSADRAIN, _saved_termios)
    except Exception:
        # Atexit não pode falhar — logger pode já estar fechado, mas tentamos.
        try:
            logger.debug("termios restore failed", exc_info=True)
        except Exception:
            pass
    finally:
        _saved_termios = None
        _termios_fd = -1


def prime_termios_snapshot(original_termios=None) -> None:
    """Captura (ou registra) o snapshot termios *original* (cooked).

    M13 (PR #295 review): o CLI principal já chamou ``setcbreak`` quando o
    painel pede o claim, então um ``tcgetattr`` agora devolveria estado
    cbreak — atexit restauraria cbreak (não cooked) e deixaria o terminal
    quebrado. Para resolver, o caller (CLI) deve invocar esta função
    ANTES de qualquer ``setcbreak`` própria, passando o snapshot original
    capturado naquele momento (``original_termios``). Quando ``None``,
    captura o snapshot atual.

    Iter-2 review: quando ``original_termios`` é ``None`` e o terminal já
    está em cbreak (ICANON desligado), RECUSAMOS capturar — capturar
    cbreak como "original" deixaria atexit restaurando cbreak (sem echo
    nem line-editing). Caller deve passar o snapshot cooked explícito.
    Emitimos um logger.warning para sinalizar.

    Idempotente: chamadas após o snapshot já estar registrado são no-op
    (preservamos o snapshot mais antigo, que é o realmente original).
    """
    global _saved_termios, _termios_fd, _atexit_registered

    with _lock:
        if _saved_termios is None and sys.stdin.isatty():
            try:
                import termios

                fd = sys.stdin.fileno()
                if original_termios is not None:
                    _saved_termios = original_termios
                    _termios_fd = fd
                else:
                    current = termios.tcgetattr(fd)
                    # current = [iflag, oflag, cflag, lflag, ispeed, ospeed, cc]
                    # ICANON está em lflag (index 3). Se OFF, terminal já está
                    # em cbreak — capturar agora gravaria o estado "errado".
                    lflag = current[3] if len(current) > 3 else 0
                    if not (lflag & termios.ICANON):
                        logger.warning(
                            "prime_termios_snapshot: terminal já está em cbreak "
                            "(ICANON desligado); ignorando auto-capture. "
                            "Caller deve passar o snapshot cooked explicitamente "
                            "via prime_termios_snapshot(original_termios=saved)."
                        )
                    else:
                        _saved_termios = current
                        _termios_fd = fd
            except Exception:
                logger.debug("could not capture termios snapshot", exc_info=True)

        if not _atexit_registered:
            try:
                atexit.register(_restore_termios)
                _atexit_registered = True
            except Exception:
                pass


def claim_stdin_for_panel(original_termios=None) -> None:
    """Painel anuncia leitura exclusiva de stdin.

    Na primeira chamada do processo, captura os termios originais (ou usa o
    snapshot passado em ``original_termios`` — M13 — PR #295 review) e
    registra o handler atexit. As chamadas seguintes só (re-)setam o event.

    Também faz ``tcflush(TCIFLUSH)`` para descartar bytes pendentes que o
    CLI possa ter colocado no buffer ANTES da exclusão entrar em vigor
    (fecha a janela TOCTOU do A2 da revisão).

    Args:
        original_termios: snapshot capturado ANTES de o CLI entrar em cbreak.
            Quando ``None`` (default), captura o estado atual — caminho legado
            que pode capturar cbreak se o CLI já modificou. Prefira passar
            explicitamente o snapshot cooked.
    """
    prime_termios_snapshot(original_termios=original_termios)
    _panel_owns_stdin.set()

    # Descarta bytes pendentes do buffer de entrada — fecha TOCTOU window
    # onde o CLI watcher poderia ter feito read entre o usuário pressionar
    # a tecla e o claim() entrar em vigor.
    if sys.stdin.isatty():
        try:
            import termios

            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except Exception:
            pass


def release_stdin_for_panel() -> None:
    """Painel devolve stdin pro CLI.

    Não restaura termios — o painel é executado entre turnos da CLI; o CLI
    cuida do seu próprio ciclo cbreak/restore. ``_saved_termios`` permanece
    capturado para o atexit fallback (Ctrl+C no meio do painel).
    """
    _panel_owns_stdin.clear()


def panel_owns_stdin() -> bool:
    """CLI consulta antes de cada read pra saber se deve pausar."""
    return _panel_owns_stdin.is_set()


def restore_termios_now() -> None:
    """Restaura termios manualmente — exposto para handlers de SIGINT/Ctrl+C
    no shutdown da CLI ou para testes que queiram limpar estado.
    """
    _restore_termios()


__all__ = [
    "claim_stdin_for_panel",
    "panel_owns_stdin",
    "prime_termios_snapshot",
    "release_stdin_for_panel",
    "restore_termios_now",
]
