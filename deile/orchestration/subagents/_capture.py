"""Stdio capture machinery for ``SubAgentOrchestrator`` (SRP extract).

Extraído de :mod:`deile.orchestration.subagents.orchestrator` (item 9 — SRP):
o orquestrador agregava três responsabilidades (coordenação paralela, redirect
de stdout/stderr e captura com cap). Esta camada isola a **captura** — um
``TextIO`` write-only com limite de tamanho (``CappedBuffer``), o accessor do
cap via :mod:`deile.config.settings` (Pilar 9 — configuração centralizada),
e o lock-holder que serializa dispatches concorrentes que mutam ``sys.stdout``.

Garantias preservadas:
  * ``CappedBuffer`` mantém os primeiros ``max_bytes`` caracteres; descarta o
    resto sem quebrar ``print()`` / ``subprocess`` line-buffering. Após o
    limite, escreve uma única marca ``[...truncated]`` na primeira tentativa
    pós-cap pra deixar claro pra debug que algo foi cortado.
  * ``max_bytes=None`` (default) resolve o limite *lazy* via
    :func:`get_capture_buffer_max_bytes` — respeita overrides em runtime
    (env ``DEILE_SUBAGENT_CAPTURE_BUFFER_MAX_BYTES`` / settings).
  * ``get_capture_lock()`` retorna um :class:`asyncio.Lock` lazy-bound ao
    event loop corrente (MA5 — iter-2). O singleton
    :data:`_capture_lock_holder` é exposto como ``SubAgentOrchestrator
    ._CAPTURE_LOCK_HOLDER`` para retrocompat com testes que chamam
    ``.reset()`` por ali.

Compat:
  * ``orchestrator._CappedBuffer`` permanece exportado como alias para
    :class:`CappedBuffer` (testes importam o nome privado histórico).
  * ``orchestrator._get_capture_buffer_max_bytes`` permanece exportado como
    alias para :func:`get_capture_buffer_max_bytes`.
  * ``SubAgentOrchestrator._CAPTURE_LOCK_HOLDER`` continua sendo a mesma
    instância de :class:`LoopBoundLock` exposta como
    :data:`_capture_lock_holder` aqui.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from deile.config.settings import get_settings

from ._loop_lock import LoopBoundLock


def get_capture_buffer_max_bytes() -> int:
    """Cap do buffer de captura — lido via :mod:`deile.config.settings`.

    Sub-DEILE pode rodar ``apt install`` ou ``npm install`` que despeja MB
    de output. Sem o cap, 5 sub-DEILEs em paralelo manteriam dezenas de MB
    em RAM até o resultado ser devolvido (e o ``data`` da tool é truncado
    em ``summary[:400]`` downstream — o buffer completo é desperdício).

    Default histórico: 256 KiB. Overridable via
    ``subagent_capture_buffer_max_bytes`` em settings (env
    ``DEILE_SUBAGENT_CAPTURE_BUFFER_MAX_BYTES`` ou
    ``~/.deile/settings.json``).
    """
    return int(getattr(get_settings(), "subagent_capture_buffer_max_bytes", 256 * 1024))


class CappedBuffer:
    """``TextIO`` write-only com limite de tamanho.

    Mantém os primeiros ``max_bytes`` caracteres; descarta o resto sem
    quebrar ``print()`` / ``subprocess`` line-buffering. Após o limite,
    escreve uma única marca ``[...truncated]`` na primeira tentativa pós-cap
    pra deixar claro pra debug que algo foi cortado.

    Issue #257 round 3 — substitui o ``StringIO`` unbounded original (C5).

    ``max_bytes=None`` (default) faz o limite ser resolvido lazy via
    :func:`get_capture_buffer_max_bytes`, respeitando overrides em runtime
    (env/settings).
    """

    __slots__ = ("_chunks", "_size", "_max", "_truncated")

    def __init__(self, max_bytes: Optional[int] = None) -> None:
        if max_bytes is None:
            max_bytes = get_capture_buffer_max_bytes()
        self._chunks: list = []
        self._size: int = 0
        self._max: int = max(0, int(max_bytes))
        self._truncated: bool = False

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        n = len(s)
        if self._size >= self._max:
            if not self._truncated:
                self._chunks.append("\n[...truncated]\n")
                self._truncated = True
            return n  # report success per file protocol
        # Espaço restante; pode ser tudo ou parte.
        remaining = self._max - self._size
        if n <= remaining:
            self._chunks.append(s)
            self._size += n
        else:
            self._chunks.append(s[:remaining])
            self._chunks.append("\n[...truncated]\n")
            self._size = self._max
            self._truncated = True
        return n

    def flush(self) -> None:
        return None

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def getvalue(self) -> str:
        """Retorna conteúdo agregado — compatível com ``io.StringIO.getvalue``."""
        return "".join(self._chunks)


class _DiscardSink:
    """``TextIO``-like sink que descarta silenciosamente todo write.

    Usado como destino de fallback para o :class:`SwitchableStream` depois que
    o dispatch encerra — qualquer thread orfã que ainda tente escrever recebe
    sucesso (sem ``BrokenPipeError``, sem flooding na captura) mas o byte é
    aniquilado.
    """

    __slots__ = ()

    def write(self, s) -> int:  # noqa: D401 — file protocol
        return len(s) if isinstance(s, str) else 0

    def flush(self) -> None:
        return None

    def writelines(self, lines) -> None:
        return None

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


class SwitchableStream:
    """``TextIO``-like proxy que delega para um destino mutável.

    Substituído por :class:`SubAgentOrchestrator` no lugar de ``sys.stdout`` /
    ``sys.stderr`` durante a execução. ``set_target(target)`` troca o destino
    em tempo real **sem reatribuir** ``sys.stdout``.

    Issue #257 round-X fix (orphan-thread leak): ``asyncio.to_thread`` cria
    worker threads no pool global que **não respondem a `Task.cancel()`** —
    quando o budget estoura ou o caller cancela, o orquestrador cancela a
    task de await mas a thread continua rodando ``execute_sync`` (ex.:
    ``bash_tool`` no meio de ``subprocess.run`` longo). O ``finally`` então
    restaurava ``sys.stdout = prev_stdout``; a thread orfã, ao chamar
    ``print(data, ...)`` em sequência, escrevia direto no terminal real.

    Solução: nunca reatribuir ``sys.stdout`` para o stream original. Em vez
    disso, instalar este wrapper ``UMA VEZ``; trocar ``target`` para o buffer
    de captura no início, e para o **stream real** ou para :class:`_DiscardSink`
    no encerramento. Threads orfãs que herdaram a referência ao
    ``SwitchableStream`` continuam escrevendo nele sem afetar o terminal.

    No ``release()`` chamado pelo orquestrador, o caller decide pra onde os
    writes pós-dispatch devem ir: ``DISCARD`` para silenciá-los (default em
    ``capture_output=True``) ou ``PASSTHROUGH`` para deixá-los aparecer no
    terminal (caso onde o caller quer transparência total para writes legítimos
    do CLI principal após o dispatch terminar — explicado abaixo).

    Threading: ``set_target`` é atômico (assignment de atributo é GIL-protegido
    em CPython); leituras em ``write`` veem o valor mais recente ou um valor
    consistente anterior. Aceita-se a janela em que uma thread orfã pode
    escrever no ``_CappedBuffer`` por mais alguns writes após ``release()`` —
    é exatamente o resultado que queremos (não vazar para o terminal).
    """

    __slots__ = ("_target", "_real")

    def __init__(self, real_stream) -> None:
        # ``_real`` é o stream original do processo (terminal/pipe). Mantido para
        # restauração no modo PASSTHROUGH, isatty/encoding fallback e o painel
        # construir seu próprio Console com ``file=real_stream``.
        self._real = real_stream
        self._target = real_stream

    @property
    def real(self):
        """O stream original que envelopamos — usado pelo painel para
        renderizar diretamente no terminal mesmo enquanto a captura está
        ativa."""
        return self._real

    def set_target(self, target) -> None:
        """Troca o destino atual atomicamente. ``target`` deve ter ``write``."""
        self._target = target

    def write(self, s) -> int:
        try:
            return self._target.write(s)
        except (BrokenPipeError, ValueError):
            # ``ValueError`` cobre StringIO já fechado em pytest teardown;
            # ``BrokenPipeError`` quando o terminal real foi fechado.
            return len(s) if isinstance(s, str) else 0

    def flush(self) -> None:
        try:
            self._target.flush()
        except (BrokenPipeError, ValueError):
            pass

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def isatty(self) -> bool:
        try:
            return self._real.isatty()
        except Exception:
            return False

    @property
    def encoding(self) -> str:
        return getattr(self._real, "encoding", "utf-8") or "utf-8"

    @property
    def buffer(self):
        # Algumas libs (Rich, prompt_toolkit) consultam ``sys.stdout.buffer``
        # para escrever bytes crus. Expõe o buffer do stream real — esses
        # writes BYPASSAM a captura intencionalmente (são para uso de coisas
        # como o painel que JÁ queremos no terminal). Sub-DEILEs não usam
        # ``buffer`` diretamente; ``print()`` e ``subprocess`` passam por
        # ``write``, que é o caminho redirecionado.
        return getattr(self._real, "buffer", None)

    def fileno(self) -> int:
        # Quando o destino é o real stream, retorna fd dele; quando é o
        # _CappedBuffer, levanta UnsupportedOperation como qualquer StringIO.
        # Subprocessos que herdam fds não passam por aqui — usam o fd 1
        # diretamente, fora do nosso controle.
        return self._real.fileno()


# Singleton ``LoopBoundLock`` que serializa dispatches do orquestrador com
# ``capture_output=True``. Vive aqui (módulo de captura) em vez de attribute
# da classe ``SubAgentOrchestrator`` — a classe ainda expõe o atributo
# ``_CAPTURE_LOCK_HOLDER`` apontando para esta instância (retrocompat com
# testes que chamam ``SubAgentOrchestrator._CAPTURE_LOCK_HOLDER.reset()``).
_capture_lock_holder: LoopBoundLock = LoopBoundLock()


def get_capture_lock() -> asyncio.Lock:
    """Retorna o ``asyncio.Lock`` lazy-bound ao event loop corrente.

    Delega a :class:`LoopBoundLock`, que cria/troca o Lock conforme o
    loop muda — evitando ``RuntimeError: ... is bound to a different
    event loop`` em múltiplos ``asyncio.run()`` (CLI sub-comandos,
    pytest loop-per-test).
    """
    return _capture_lock_holder.get()


__all__ = [
    "CappedBuffer",
    "SwitchableStream",
    "_DiscardSink",
    "get_capture_buffer_max_bytes",
    "get_capture_lock",
    "_capture_lock_holder",
]
