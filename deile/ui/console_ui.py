import random
import time
from pathlib import Path
from typing import List, Optional, Dict, Any

import yaml

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.status import Status
from rich.prompt import Prompt as RichPrompt
from rich.table import Table
from rich import box

from prompt_toolkit import PromptSession

from .base import UIManager, UITheme, UIMessage, MessageType, UIStatus
from .completers import HybridCompleter
from .markdown_table import DeileMarkdown as Markdown


class ConsoleUIManager(UIManager):
    """Implementação da UI de console usando Rich e prompt_toolkit."""

    def __init__(self, theme: UITheme = UITheme.DEFAULT, config_manager=None):
        super().__init__(theme)
        import sys

        # Force UTF-8 stdout encoding when possible (helps emoji/box chars on Windows).
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except Exception:
                pass

        # Detect a true legacy Windows console (cmd.exe / older conhost without
        # ANSI support). On modern Windows (Windows Terminal, VSCode, ConEmu)
        # ANSICON / WT_SESSION / TERM are populated, so Rich's auto-detection
        # works correctly. Forcing ``legacy_windows=True`` globally — as the
        # previous implementation did — disabled Live refresh + Markdown
        # rendering on every platform (notably macOS/Linux), breaking the
        # streaming UI's Markdown output entirely. We now only opt into legacy
        # behavior when the OS is Windows AND no modern-terminal hint is
        # present.
        import os as _os
        _force_legacy_windows = (
            _os.name == 'nt'
            and not _os.environ.get('WT_SESSION')
            and not _os.environ.get('ANSICON')
            and not _os.environ.get('ConEmuPID')
            and (_os.environ.get('TERM') in (None, '', 'cygwin'))
        )

        if _force_legacy_windows:
            self.console = Console(
                force_terminal=True,
                legacy_windows=True,
                _environ={"TERM": "ansi"},
                file=sys.stdout,
            )
        else:
            # Let Rich auto-detect color depth, ANSI support, and width.
            # ``force_terminal=True`` keeps colors when stdout is wrapped
            # (e.g. captured by tests) but does not disable ANSI sequences.
            self.console = Console(
                force_terminal=True,
                file=sys.stdout,
            )
        self.session: Optional[PromptSession] = None
        self.is_initialized = False
        self.config_manager = config_manager
        self.working_directory = None

    def initialize(self) -> None:
        """Inicializa a UI."""
        self.is_initialized = True

    def setup_file_completion(self, file_paths: List[str]) -> None:
        """Configura autocompletar (compatibilidade) - usa HybridCompleter."""
        self.setup_hybrid_completion(working_directory=self.working_directory)
    
    def setup_hybrid_completion(self, working_directory: Optional[str] = None) -> None:
        """Configura o HybridCompleter para @ (arquivos) e / (comandos)."""
        if working_directory:
            self.working_directory = working_directory
        
        # SOLUÇÃO ROBUSTA: Fallback completo para Windows
        try:
            # Primeiro tenta verificar se estamos em um terminal compatível
            # import os
            # if os.name == 'nt' and 'TERM' not in os.environ:
            #     # Windows sem terminal ANSI adequado - usa fallback direto
            #     self.session = None
            #     return
                
            hybrid_completer = HybridCompleter(
                config_manager=self.config_manager,
                working_directory=self.working_directory
            )
            
            # Tenta configuração mais compatível com Windows
            import asyncio
            from prompt_toolkit import PromptSession
            from prompt_toolkit.output import ColorDepth
            from prompt_toolkit.key_binding import KeyBindings
            from prompt_toolkit.formatted_text import FormattedText
            from prompt_toolkit.application import get_app
            from prompt_toolkit.layout.containers import Window, ConditionalContainer
            from prompt_toolkit.layout.controls import FormattedTextControl
            from prompt_toolkit.filters import Condition

            kb = KeyBindings()
            _esc = {'active': False, 'task': None}

            def _hide_esc_hint(event):
                _esc['active'] = False
                if _esc['task']:
                    _esc['task'].cancel()
                    _esc['task'] = None
                event.app.invalidate()

            def _insert_newline(event):
                _hide_esc_hint(event)
                event.current_buffer.insert_text("\n")

            @kb.add("escape", "enter", eager=True)
            @kb.add("escape", "c-j", eager=True)
            def _on_alt_enter(event):
                _insert_newline(event)

            @kb.add("escape")
            def _on_esc(event):
                if not event.current_buffer.text:
                    _hide_esc_hint(event)
                    return

                if _esc['active']:
                    event.current_buffer.reset()
                    _hide_esc_hint(event)
                    return

                _esc['active'] = True
                event.app.invalidate()

                async def _auto_hide():
                    await asyncio.sleep(1.5)
                    _esc['active'] = False
                    _esc['task'] = None
                    event.app.invalidate()

                if _esc['task']:
                    _esc['task'].cancel()
                _esc['task'] = event.app.create_background_task(_auto_hide())

            def _get_cols():
                try:
                    return get_app().output.get_size().columns
                except Exception:
                    return 80

            # User-input visual contract: ALWAYS render the input area with
            # black background + white text so the user's own message is
            # unambiguously distinguishable from agent output, regardless of
            # the terminal's color scheme.
            from prompt_toolkit.styles import Style as PTStyle
            _USER_INPUT_STYLE = 'bg:#000000 #ffffff'
            user_style = PTStyle.from_dict({
                '': _USER_INPUT_STYLE,             # default buffer text
                'prompt': _USER_INPUT_STYLE,
                'separator': _USER_INPUT_STYLE,
            })

            # The visual divider that separates turns is intentionally NOT
            # part of the prompt's message — it would re-render on every
            # SIGWINCH (terminal resize), and prompt_toolkit's height
            # accounting can underestimate how many lines to clear when the
            # column count changes, leaving stale dividers stacked in
            # scrollback. Instead we print the divider once via
            # ``console.rule()`` immediately before invoking the prompt
            # (see ``get_user_input``); the static rule lives in scrollback
            # and is never redrawn, so resize is harmless.
            def _prompt_message():
                return FormattedText([
                    ('class:prompt', '> '),
                ])

            self.session = PromptSession(
                message=_prompt_message,
                completer=hybrid_completer,
                complete_while_typing=True,
                color_depth=ColorDepth.DEPTH_1_BIT,
                key_bindings=kb,
                style=user_style,
            )

            # Inject ESC hint directly below the input area.
            # The top separator (from _prompt_message) is the only divider — a bottom
            # separator would persist in scrollback after submission because
            # prompt_toolkit reserves the row for ConditionalContainer even when the
            # filter collapses to false on is_done.
            hint_win = ConditionalContainer(
                content=Window(
                    height=1,
                    content=FormattedTextControl(
                        lambda: FormattedText([('fg:ansibrightblack', ' Esc novamente para limpar')])
                    ),
                    dont_extend_height=True,
                ),
                filter=Condition(lambda: _esc['active']),
            )
            root = self.session.app.layout.container
            if hasattr(root, 'children') and isinstance(root.children, list):
                root.children.insert(1, hint_win)

            # sleep(0) = next event-loop tick — instant ESC flush with no perceptible delay.
            # ANSI sequences (arrows etc.) arrive as a burst in one read(), so they're safe.
            self.session.app.ttimeoutlen = 0
            self.session.app.timeoutlen = 0
            
        except Exception as e:
            # Fallback completo - sem prompt_toolkit 
            self.session = None

    _DEILE_ASCII = r"""
 ██████╗  ███████╗ ██╗ ██╗      ███████╗
 ██╔══██╗ ██╔════╝ ██║ ██║      ██╔════╝
 ██║  ██║ █████╗   ██║ ██║      █████╗
 ██║  ██║ ██╔══╝   ██║ ██║      ██╔══╝
 ██████╔╝ ███████╗ ██║ ███████╗ ███████╗
 ╚═════╝  ╚══════╝ ╚═╝ ╚══════╝ ╚══════╝"""

    _SLOGAN_FIXED = "I don't sleep. I don't hesitate."
    _SLOGAN_POOL = [
        "You dream it. I code it.",
        "You dream it. I build it.",
        "You imagine. I execute.",
        "No sleep. No doubt. Just code.",
        "You bring the vision. I bring the code.",
        "You dream. I compile reality.",
        "Ideas in. Code out.",
        "Think it. Prompt it. Ship it.",
    ]

    _PROVIDER_LABELS = {
        "deepseek": "DeepSeek",
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "gemini": "Gemini",
        "google": "Gemini",
    }

    def _resolve_provider_model(self) -> tuple[str, str]:
        """Lê provider/modelo correntes a partir do config_manager."""
        try:
            if self.config_manager:
                cfg = self.config_manager.get_config()
                default_model = getattr(cfg, "default_model", None)
                if default_model and ":" in default_model:
                    provider_id, model_id = default_model.split(":", 1)
                    label = self._PROVIDER_LABELS.get(provider_id.lower(), provider_id)
                    return label, model_id
                if default_model:
                    return "—", default_model
                try:
                    yaml_path = Path(__file__).parents[1] / "config" / "model_providers.yaml"
                    with open(yaml_path) as f:
                        strategy = yaml.safe_load(f).get("default_strategy", "task_optimized")
                    return "Auto", f"routing ({strategy})"
                except Exception:
                    return "Auto", "routing"
        except Exception:
            pass
        return "—", "—"

    def show_welcome(self):
        """Mostra a tela de boas-vindas formatada."""
        self.console.clear()

        provider_label, model_label = self._resolve_provider_model()
        slogan_random = random.choice(self._SLOGAN_POOL)

        try:
            self.console.print(
                Text(self._DEILE_ASCII, style="bold #4285F4"),
                highlight=False,
            )
            self.console.print(
                Text.from_markup(f"\n  [bold #FFD166]✦[/] [italic]{self._SLOGAN_FIXED}[/italic]")
            )
            self.console.print(
                Text.from_markup(f"  [bold #FFD166]✦[/] [italic]{slogan_random}[/italic]\n")
            )

            border = "#4285F4"

            prov_label = f"Provider  {provider_label}"
            model_label_line = f"Model     {model_label}"
            status_plain = f"● DEILE   Pronto — digite /help para começar"

            inner_w = max(len(prov_label), len(model_label_line), len(status_plain)) + 2

            def _row(content_markup: str, visible_len: int) -> Text:
                pad = " " * max(0, inner_w - 1 - visible_len)
                line = Text()
                line.append("║", style=border)
                line.append_text(Text.from_markup(" " + content_markup + pad))
                line.append("║", style=border)
                return line

            top = Text("╔" + "═" * inner_w + "╗", style=border)
            mid = Text("╠" + "═" * inner_w + "╣", style=border)
            bot = Text("╚" + "═" * inner_w + "╝", style=border)

            prov_markup = f"[bold cyan]Provider[/bold cyan]  [white]{provider_label}[/white]"
            model_markup = f"[bold cyan]Model[/bold cyan]     [white]{model_label}[/white]"
            status_markup = "[bold green]●[/bold green] [bold]DEILE[/bold]   Pronto — digite [cyan]/help[/cyan] para começar"

            self.console.print(top)
            self.console.print(_row(prov_markup, len(prov_label)))
            self.console.print(_row(model_markup, len(model_label_line)))
            self.console.print(mid)
            self.console.print(_row(status_markup, len(status_plain)))
            self.console.print(bot)
            self.console.print("  [dim]DEILE v5.1 ULTRA[/dim]\n")
        except Exception:
            print("DEILE v5.1 ULTRA")
            print(f"  ✦ {self._SLOGAN_FIXED}")
            print(f"  ✦ {slogan_random}")
            print(f"Provider: {provider_label} | Model: {model_label}")
            print("Pronto — digite /help para começar")

    def get_user_input(self, prompt: str = "\n [bold green]>[/bold] ") -> str:
        """Obtém a entrada do usuário de forma interativa."""
        if not self.session:
            clean_prompt = prompt.replace("[bold green]", "").replace("[/bold]", "").replace("[/]", "")
            if not clean_prompt.startswith("\n"):
                clean_prompt = "\n" + clean_prompt
            return input(clean_prompt)

        # Static turn separator — rendered once into scrollback per turn so
        # terminal resize cannot duplicate it (see _prompt_message comment).
        self.console.rule(style="dim")
        try:
            return self.session.prompt()
        except Exception:
            return input('> ')

    def display_response(self, content, metadata: Optional[Dict] = None):
        """Exibe a resposta do agente com metadados."""
        self.console.print("\n[bold #4285F4]Deile >[/] ")
        
        # Verifica se é um objeto Rich (Panel, Table, etc.)
        if hasattr(content, '__rich__') or hasattr(content, '__rich_console__'):
            # É um objeto Rich - renderiza diretamente
            self.console.print(content)
        elif isinstance(content, str):
            # É string - usa Markdown
            self.console.print(Markdown(content))
        else:
            # Fallback - converte para string
            self.console.print(str(content))
        
        if metadata and (exec_time := metadata.get("execution_time")) is not None:
            model_used = metadata.get("model_used") or ""
            model_suffix = f"  [dim]({model_used})[/dim]" if model_used else ""
            self.console.print(f"\n:hourglass: [dim]{exec_time:.2f}s[/dim]{model_suffix}")

    def display_message(self, message: UIMessage):
        """Exibe uma mensagem simples com base no seu tipo."""
        # CORREÇÃO 2: A função console.print() já interpreta markup por padrão.
        # Remover o argumento 'style' permite que os emojis e cores no próprio texto sejam renderizados.
        self.console.print(message.content)

    def display_error(self, error: str, details: Optional[str] = None):
        """Exibe um erro formatado com fallback robusto."""
        try:
            # Primeira tentativa com emoji
            error_text = Text.from_markup(f":x: [bold red]ERRO:[/bold red] {error}")
            panel = Panel(error_text, border_style="red", title="[bold red]Ocorreu um Problema[/bold red]")
            if details:
                panel.renderable = Text.from_markup(f":x: [bold red]ERRO:[/bold red] {error}\n\n[dim]{details}[/dim]")
            self.console.print(panel)
        except (UnicodeEncodeError, Exception) as e:
            try:
                # Fallback sem emoji
                error_text = Text.from_markup(f"[!] [bold red]ERRO:[/bold red] {error}")
                panel = Panel(error_text, border_style="red", title="[bold red]Ocorreu um Problema[/bold red]")
                if details:
                    panel.renderable = Text.from_markup(f"[!] [bold red]ERRO:[/bold red] {error}\n\n[dim]{details}[/dim]")
                self.console.print(panel)
            except Exception:
                # Fallback final - texto simples
                try:
                    print("=" * 50)
                    print("ERRO:", error)
                    if details:
                        print("Detalhes:", details)
                    print("=" * 50)
                except:
                    print("ERRO ocorreu na exibição")

    def show_loading(self, message: str) -> Status:
        """Mostra uma animação de 'carregando' de forma segura."""
        return self.console.status(f"[bold cyan]{message}[/]", spinner="line")

    async def display_streaming_turn(self, event_stream) -> "RenderResult":  # type: ignore[name-defined]
        """Render a streaming agent turn progressively to the console.

        Consumes a ``UnifiedStreamEvent`` async iterator and lets the
        ``StreamingRenderer`` paint text deltas + tool blocks as they arrive.
        Returns the captured ``RenderResult`` so callers can inspect the final
        text and tool counts.

        ``legacy_windows`` is read from ``self.console`` — Rich auto-detects
        true legacy Windows consoles (cmd.exe / older conhost without ANSI),
        and on macOS/Linux/modern Windows terminals it stays ``False`` so the
        ``Live``-based Markdown rendering path is used.
        """
        from .streaming_renderer import StreamingRenderer
        renderer = StreamingRenderer(
            console=self.console,
            legacy_windows=bool(getattr(self.console, "legacy_windows", False)),
            markdown=True,
            refresh_per_second=12.0,
        )
        return await renderer.render(event_stream)

    def display_success(self, message: str) -> None:
        """Exibe uma mensagem de sucesso formatada."""
        self.console.print(f":white_check_mark: [green]{message}[/green]")

    def display_status(self, status: UIStatus) -> None:
        """Exibe uma mensagem de status simples."""
        self.console.print(f":information: [cyan]STATUS:[/cyan] {status.message}")

    def confirm_action(self, message: str, default: bool = False) -> bool:
        """Solicita confirmação do usuário para uma ação."""
        return RichPrompt.ask(f"[bold yellow]{message}[/bold yellow]", choices=["s", "n"], default="n" if not default else "s").lower() == 's'

    def display_stats(self, stats: Dict[str, Any]) -> None:
        """Exibe as estatísticas do sistema em uma tabela."""
        table = Table(box=box.ROUNDED, title="[bold]Estatísticas do DEILE[/bold]")
        table.add_column("Métrica", style="cyan")
        table.add_column("Valor", style="magenta")

        for key, value in stats.items():
            if isinstance(value, dict):
                table.add_row(f"[bold]{key.replace('_', ' ').title()}[/bold]", "")
                for sub_key, sub_value in value.items():
                    table.add_row(f"  {sub_key.replace('_', ' ').title()}", str(sub_value))
            else:
                table.add_row(key.replace('_', ' ').title(), str(value))
        
        self.console.print(table)

    def cleanup(self) -> None:
        """Realiza a limpeza de recursos da UI ao sair."""
        pass