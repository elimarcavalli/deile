"""Helpers para renderização adaptativa ao redimensionamento do terminal.

Issue #307 — regra raiz: **DEILE tem layout dinâmico em todos os seus
recursos**. Construções estáticas no scrollback (via `console.print()` puro)
não reflowam quando o usuário redimensiona o terminal — esse é o limite
fundamental que `rich.live.Live` quebra.

## Como `Live` resolve resize-em-tempo-real

Rich `Live`:

1. Instala um handler `SIGWINCH` internamente (Unix; Windows usa polling
   de `Console.size`).
2. A cada refresh frame (`refresh_per_second`), consulta `console.size` e
   re-renderiza o conteúdo já passando pela nova largura — sem destruir o
   scrollback acima da região Live.
3. Quando o context manager `Live` sai (`__exit__`), o ÚLTIMO frame fica
   "commitado" no scrollback (com `transient=False`) — esse frame final
   sofre da limitação fundamental: redimensionamentos depois desse momento
   não reflowam.

A solução enterprise definitiva (Textual framework) está documentada em
issue separada — esses helpers cobrem o nível 1 pragmático: surfaces
críticas (welcome, comandos pesados) adaptam dinamicamente DURANTE seu
tempo de vida.

## Quando usar

| Surface | Padrão |
|---|---|
| Tela de boas-vindas | `live_for(welcome, duration_s=8)` — fica vivo enquanto o usuário lê |
| Tabela de `/status`, `/logs`, `/cost` | `live_for(table, duration_s=2)` — adapta durante a "primeira leitura" |
| Painel de erro persistente | `live_for(panel, duration_s=4)` |
| Stream do LLM | já é Live (`streaming_renderer._render_live`) |

## Quando NÃO usar

- Output non-TTY (pipe, CI, `>` redirect): cai em `console.print()` estático
  via `is_interactive_tty()` (Live precisa de TTY pra cursor positioning).
- Conteúdo que deve ficar permanente sem ser "tocado de novo" (logs de
  auditoria, exports `.txt`).
"""

from __future__ import annotations

import os
import sys
import time
from typing import AsyncIterator, Callable, List, Union

from rich.console import Console, RenderableType
from rich.live import Live
from rich.text import Text

# Default refresh rate: 8 frames per second.
# Suficiente para detectar resize via SIGWINCH em < 130ms; baixo o
# bastante pra não consumir CPU notável.
_DEFAULT_REFRESH_HZ = 8.0


def is_interactive_tty() -> bool:
    """Retorna ``True`` quando podemos usar ``Live`` com segurança.

    Live requer cursor positioning (ANSI), que só funciona em terminais
    interativos. Em pipe/redirect/CI, caímos em fallback estático.
    """
    try:
        return bool(sys.stdout.isatty() and os.environ.get("TERM") not in ("dumb", ""))
    except Exception:
        return False


def live_for(
    renderable: Union[RenderableType, Callable[[], RenderableType]],
    *,
    console: Console,
    duration_s: float = 3.0,
    refresh_hz: float = _DEFAULT_REFRESH_HZ,
) -> None:
    """Renderiza ``renderable`` dentro de uma região ``Live`` por ``duration_s``.

    Durante o período, Rich re-renderiza ``refresh_hz`` vezes por segundo,
    capturando `SIGWINCH` automaticamente. Cada frame consulta
    ``console.size`` corrente — resize é refletido em < 130ms (com
    refresh_hz=8).

    Aceita ``renderable`` estático ou ``Callable[[], RenderableType]``. Use
    callable quando o conteúdo deve ser RE-construído a cada frame (ex.: o
    welcome screen com slogan rotativo, ou painéis com timestamp).

    Args:
        renderable: O conteúdo Rich a renderizar (Panel, Table, Group,
            Markdown, etc.) ou função que devolve o conteúdo.
        console: Console Rich onde renderizar.
        duration_s: Por quanto tempo manter o Live ativo. Após esse
            período, sai e o último frame fica commitado no scrollback.
            Default: 3 segundos.
        refresh_hz: Frames por segundo. Default: 8.

    Fallback non-TTY: se ``is_interactive_tty()`` é False, faz uma
    chamada única de ``console.print()`` e retorna.
    """
    if not is_interactive_tty():
        item = renderable() if callable(renderable) else renderable
        console.print(item)
        return

    end_at = time.monotonic() + duration_s
    period = 1.0 / refresh_hz

    item_initial = renderable() if callable(renderable) else renderable
    with Live(
        item_initial,
        console=console,
        refresh_per_second=refresh_hz,
        transient=False,
        auto_refresh=False,
    ) as live:
        while time.monotonic() < end_at:
            current = renderable() if callable(renderable) else renderable
            live.update(current)
            live.refresh()
            time.sleep(period)
        # Final frame com a largura atual — quando Live sair, esse frame
        # fica no scrollback.
        final = renderable() if callable(renderable) else renderable
        live.update(final)
        live.refresh()


async def live_stream(
    line_iterator: AsyncIterator[str],
    *,
    console: Console,
) -> List[str]:
    """Stream lines via Rich Live com reflow. Retorna lista de todas as linhas recebidas.

    Em TTY interativo, usa ``rich.live.Live`` com ``auto_refresh=False`` para
    renderizar cada linha recebida, possibilitando reflow enquanto o conteúdo
    chega. Em ambiente não-TTY (pipe/CI/redirect), imprime cada linha via
    ``console.print`` diretamente.

    Nota: esta função NÃO usa ``live_for`` — ``live_for`` é para conteúdo com
    duração fixa, não para streaming de saída de subprocesso.

    Args:
        line_iterator: AsyncIterator que produz strings (uma por linha).
        console: Console Rich onde renderizar.

    Returns:
        Lista de todas as linhas recebidas, na ordem de chegada.
    """
    all_lines: List[str] = []

    if is_interactive_tty():
        with Live(
            Text(""),
            console=console,
            auto_refresh=False,
            transient=False,
        ) as live:
            async for line in line_iterator:
                all_lines.append(line)
                live.update(Text("\n".join(all_lines)))
                live.refresh()
    else:
        async for line in line_iterator:
            all_lines.append(line)
            console.print(line)

    return all_lines


def turn_separator(console: Console, *, style: str = "#4285F4 dim") -> None:
    """Imprime um separador horizontal demarcando turnos do chat.

    Usado entre input do usuário e resposta do assistente — torna o fluxo
    do chat mais legível, especialmente em sessões longas com muitas
    interações.

    Adapta naturalmente a resize: ``console.rule()`` consulta
    ``console.width`` no momento da renderização.
    """
    console.rule(style=style)
