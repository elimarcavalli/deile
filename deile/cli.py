"""DEILE CLI entry point — `deile` command available from any directory.

Usage:
    deile                         # interactive mode
    deile "your message"         # one-shot mode
    deile --model PROVIDER:ID "msg"

When installed via pip (pip install -e .), the `deile` command uses the
*current working directory* as the agent's working directory, allowing you
to invoke DEILE anywhere:

    cd ~/my-project
    deile "analyze this codebase"
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

# ── package root (where deile/ lives) ───────────────────────────────────────
_PACKAGE_ROOT = Path(__file__).parent.resolve()
_PROJECT_ROOT = _PACKAGE_ROOT.parent  # repo root when editable, same when installed
_ENV_KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY")
_TTY = sys.stdout.isatty()
_RESET = "\033[0m" if _TTY else ""
_BOLD = "\033[1m" if _TTY else ""
_DIM = "\033[2m" if _TTY else ""
_GREEN = "\033[0;32m" if _TTY else ""


def _find_dotenv() -> Optional[Path]:
    """Look for .env in cwd, then home, then project root."""
    candidates = [
        Path.cwd() / ".env",
        Path.home() / ".deile.env",
        _PROJECT_ROOT / ".env",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_dotenv() -> None:
    env_file = _find_dotenv()
    if env_file:
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
        except ImportError:
            pass


def _silence_genai_shutdown_noise() -> None:
    """Make `google.genai.Client.__del__` defensive (no AttributeError at shutdown)."""
    try:
        from google.genai import client as _gc
    except ImportError:
        return
    original_del = _gc.Client.__del__

    def _safe_del(self: object) -> None:
        try:
            original_del(self)
        except Exception:
            pass

    _gc.Client.__del__ = _safe_del


def _run_env_recovery() -> bool:
    """Interactive wizard: prompt for API keys, write .env, reload os.environ.

    Only runs when stdin is a TTY. Merges with any existing .env so unrelated
    variables (DEILE_* settings, etc.) are preserved. Returns True if at least
    one key was saved.
    """
    if not sys.stdin.isatty():
        return False

    import getpass

    env_path = _find_dotenv() or (_PROJECT_ROOT / ".env")

    print()
    print(f"  {_BOLD}Chaves de API{_RESET}")
    print(f"  {_DIM}Pelo menos UMA é necessária para iniciar o DEILE.{_RESET}")
    print(f"  {_DIM}Pressione ENTER para pular ou manter o valor atual.{_RESET}")
    print()

    new_keys: dict[str, str] = {}
    for name in _ENV_KEY_NAMES:
        current = os.getenv(name, "")
        suffix = f" {_DIM}[já configurado]{_RESET}" if current else ""
        try:
            val = getpass.getpass(f"  {name}{suffix}: ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return False
        new_keys[name] = val or current

    if not any(new_keys.values()):
        return False

    kept_lines: list[str] = []
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if "=" in stripped and not stripped.startswith("#"):
                k = stripped.split("=", 1)[0].strip()
                if k in _ENV_KEY_NAMES:
                    continue
            kept_lines.append(raw)
    except FileNotFoundError:
        pass

    new_lines = kept_lines + [f"{k}={v}" for k, v in new_keys.items() if v]
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass

    try:
        from dotenv import load_dotenv as _ld
        _ld(env_path, override=True)
    except ImportError:
        for k, v in new_keys.items():
            if v:
                os.environ[k] = v

    count = sum(1 for v in new_keys.values() if v)
    print(f"\n  {_GREEN}✓{_RESET}  {count} chave(s) salva(s) em {env_path}\n")
    return True


# ── interactive mode ────────────────────────────────────────────────────────

class _DeileCLI:
    """Thin wrapper that reuses the DEILE agent + UI stack."""

    def __init__(self) -> None:
        self.settings: object = None
        self.agent: object = None
        self.default_session: object = None
        self.ui: object = None
        self.config_manager: object = None

    def _bootstrap_providers(self, model_router) -> list:
        """Bootstrap model providers, preferring the new path with legacy fallback.

        NOTE: this is a *synchronous* method — it does not await anything.
        It runs in the current thread (not via asyncio.to_thread) to avoid
        the "coroutine was never awaited" warning.
        """
        # check feature flag
        try:
            import yaml
            yaml_path = _PACKAGE_ROOT / "config" / "model_providers.yaml"
            with open(yaml_path) as f:
                data = yaml.safe_load(f)
            if bool(data.get("feature_flags", {}).get("use_legacy_gemini_only", False)):
                if os.getenv("GOOGLE_API_KEY"):
                    from deile.core.models.gemini_provider import \
                        GeminiProvider
                    model_router.register_provider(GeminiProvider(), priority=1)
                    return ["gemini"]
                return []
        except Exception:
            pass

        from deile.core.models.bootstrap import bootstrap_providers
        return bootstrap_providers(router=model_router)

    async def initialize(self) -> bool:
        from deile.config.manager import ConfigManager
        from deile.config.settings import get_settings
        from deile.core.agent import DeileAgent
        from deile.core.models.router import get_model_router
        from deile.parsers.registry import get_parser_registry
        from deile.tools.registry import get_tool_registry
        from deile.ui import ConsoleUIManager, UITheme

        self.settings = get_settings()
        # Override working_directory to cwd
        self.settings.working_directory = Path.cwd()
        self.config_manager = ConfigManager()
        self.ui = ConsoleUIManager(UITheme.DEFAULT, config_manager=self.config_manager)

        try:
            self.ui.initialize()
            self.config_manager.load_config()

            model_router = get_model_router()
            with self.ui.show_loading("Acordando DEILE..."):
                # _bootstrap_providers is sync — call it directly, no asyncio.to_thread
                registered = self._bootstrap_providers(model_router)

            if not registered and _run_env_recovery():
                with self.ui.show_loading("Acordando DEILE..."):
                    registered = self._bootstrap_providers(model_router)

            if not registered:
                self.ui.display_error(
                    "Nenhum provider configurado.",
                    "Defina ao menos uma variável de ambiente: "
                    "ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, GOOGLE_API_KEY.",
                )
                return False

            with self.ui.show_loading("Finalizando inicialização..."):
                self.agent = DeileAgent(
                    model_router=model_router,
                    tool_registry=get_tool_registry(),
                    parser_registry=get_parser_registry(),
                    config_manager=self.config_manager,
                )
                await self.agent.initialize()

                # gap #3: autostart the pipeline monitor when DEILE_PIPELINE_AUTOSTART=true
                if self.settings.pipeline_autostart:
                    await _autostart_pipeline(self.agent)

                self.default_session = self.agent.create_session(
                    session_id="default_cli_session",
                    working_directory=self.settings.working_directory,
                )

            self.ui.setup_hybrid_completion(
                working_directory=str(self.settings.working_directory)
            )
            with self.ui.show_loading("Mapeando workspace..."):
                self.ui.setup_file_completion(self._get_project_files())
            return True

        except Exception as exc:
            self.ui.display_error(
                f"Falha fatal na inicialização do agente: {exc}"
            )
            return False

    def _get_project_files(self) -> List[str]:
        files: List[str] = []
        wd = Path(self.settings.working_directory)
        ignore = {"__pycache__", ".git", "node_modules", ".venv", "venv", "dist", "build", ".deile"}
        for path in wd.rglob("*"):
            if path.is_file() and not any(d in path.parts for d in ignore):
                rel = path.relative_to(wd)
                files.append(str(rel).replace("\\", "/"))
        return sorted(files)[:500]

    async def run_interactive(self) -> None:
        from rich.panel import Panel
        from rich.text import Text

        from deile.ui import MessageType, UIMessage

        if not await self.initialize():
            return

        self.ui.show_welcome()

        try:
            while True:
                user_input = await asyncio.to_thread(self.ui.get_user_input, "\n > ")
                user_input = user_input.strip()

                if not user_input:
                    sys.stdout.write("\033[A\033[2K\r")
                    sys.stdout.flush()
                    continue

                if user_input.lower() in ("exit", "quit", "q"):
                    self.ui.display_message(UIMessage(
                        content="\n[bold yellow]DEILE se despedindo. Até a próxima! :wave:[/bold yellow]",
                        message_type=MessageType.SYSTEM,
                    ))
                    break

                streaming = getattr(self.settings, "streaming_enabled", True)
                if streaming:
                    event_stream = self.agent.process_input_stream(
                        user_input=user_input,
                        session_id=self.default_session.session_id,
                    )
                    try:
                        await self.ui.display_streaming_turn(event_stream)
                    except KeyboardInterrupt:
                        self.ui.console.print("\n[yellow](turn interrupted)[/yellow]")
                    continue

                with self.ui.show_loading("Processando sua solicitação..."):
                    response = await self.agent.process_input(
                        user_input=user_input,
                        session_id=self.default_session.session_id,
                    )

                meta = response.metadata or {}
                if meta.get("budget_exceeded"):
                    self.ui.console.print(Panel(
                        Text(f"{response.content}", style="yellow"),
                        title="[bold red]Budget Limit Reached[/bold red]",
                        border_style="red",
                        subtitle=(
                            f"provider={meta.get('provider_id', 'n/a')} • "
                            f"limit={meta.get('limit_type', 'n/a')}"
                        ),
                    ))
                elif meta.get("forced_model_not_registered"):
                    self.ui.console.print(Panel(
                        Text(f"{response.content}", style="yellow"),
                        title="[bold red]Forced Model Not Registered[/bold red]",
                        border_style="red",
                        subtitle="Use /model use auto to clear the override",
                    ))
                else:
                    self.ui.display_response(response.content, {
                        "execution_time": response.execution_time,
                        "model_used": response.metadata.get("model_used"),
                    })

                if response.tool_results and getattr(self.settings, "show_tool_details", False):
                    self.ui.console.print("\n[dim]Tool executions:[/dim]")
                    for result in response.tool_results:
                        if result.metadata and "rich_display" in result.metadata:
                            self.ui.console.print(
                                f"[dim]{result.metadata['rich_display']}[/dim]"
                            )
                        else:
                            icon = "[green]✓[/green]" if result.is_success else "[red]✗[/red]"
                            self.ui.console.print(f"[dim]{icon} {result.message}[/dim]")

        except (KeyboardInterrupt, EOFError):
            self.ui.display_message(UIMessage(
                content="\n[bold yellow]DEILE se despedindo. Até a próxima! :wave:[/bold yellow]",
                message_type=MessageType.SYSTEM,
            ))
        except Exception as exc:
            self.ui.display_error(f"Ocorreu um erro fatal no loop principal: {exc}")


# ── pipeline autostart helper ───────────────────────────────────────────────


async def _autostart_pipeline(agent) -> None:  # type: ignore[type-arg]
    """Start the pipeline monitor in the background when DEILE_PIPELINE_AUTOSTART=true.

    gap #3: operator convenience — set the env var once and every DEILE interactive
    session auto-starts the polling loop without a manual ``/pipeline start``.
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    try:
        from deile.config.settings import get_settings
        from deile.orchestration.pipeline.constants import \
            PIPELINE_DEFAULT_REPO
        from deile.orchestration.pipeline.monitor import (PipelineConfig,
                                                          PipelineMonitor)
        from deile.orchestration.pipeline.review_callback import \
            make_review_callback
        s = get_settings()
        repo = s.pipeline_repo or PIPELINE_DEFAULT_REPO
        base_path = s.pipeline_base_path
        if base_path is None:
            from pathlib import Path
            base_path = Path.cwd()
        cfg = PipelineConfig(
            repo=repo,
            base_repo_path=base_path.resolve(),
            notify_user_id=s.pipeline_notify_user_id,
        )
        monitor = PipelineMonitor(cfg, review_callback=make_review_callback(agent))
        agent.pipeline_monitor = monitor  # type: ignore[attr-defined]
        await monitor.start()
        _log.info("pipeline autostarted (repo=%s)", repo)
    except Exception as exc:  # noqa: BLE001 — autostart is best-effort; never abort CLI
        _log.warning("pipeline autostart failed: %s", exc)


# ── one-shot mode ───────────────────────────────────────────────────────────


def _print_oneshot_content(content) -> None:
    """Print response content to stdout, rendering Rich renderables properly."""
    if content is None:
        return
    if isinstance(content, str):
        print(content)
        return
    # Rich renderable (Table, Panel, Text, Group, etc.) or list thereof.
    from rich.console import Console
    console = Console()
    if isinstance(content, list):
        for item in content:
            console.print(item)
    else:
        console.print(content)


async def _run_oneshot(message: str, forced_model: Optional[str] = None) -> int:
    """Single-turn non-interactive. stdout = response.content."""
    from deile.config.manager import ConfigManager
    from deile.config.settings import get_settings
    from deile.core.agent import DeileAgent
    from deile.core.models.bootstrap import bootstrap_providers
    from deile.core.models.router import get_model_router
    from deile.parsers.registry import get_parser_registry
    from deile.tools.registry import get_tool_registry

    settings = get_settings()
    settings.working_directory = Path.cwd()
    config_manager = ConfigManager()
    config_manager.load_config()

    model_router = get_model_router()
    registered = bootstrap_providers(router=model_router)
    if not registered and _run_env_recovery():
        registered = bootstrap_providers(router=model_router)
    if not registered:
        print(
            "ERROR: no provider configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "DEEPSEEK_API_KEY, or GOOGLE_API_KEY.",
            file=sys.stderr,
        )
        return 1

    agent = DeileAgent(
        model_router=model_router,
        tool_registry=get_tool_registry(),
        parser_registry=get_parser_registry(),
        config_manager=config_manager,
    )
    await agent.initialize()

    session = agent.create_session(
        session_id="oneshot_cli_session",
        working_directory=settings.working_directory,
    )
    if forced_model:
        session.context_data["forced_model"] = forced_model
    else:
        preferred = settings.preferred_model
        if preferred:
            session.context_data["preferred_model"] = preferred

    try:
        response = await agent.process_input(
            user_input=message,
            session_id=session.session_id,
        )
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _print_oneshot_content(response.content)
    status = response.status.value if hasattr(response.status, "value") else str(response.status)
    return 0 if status != "error" else 1


def _run_self_install() -> int:
    """Install DEILE globally for the current user via pip editable mode."""
    python_exe = sys.executable
    project_root = _PROJECT_ROOT

    base_install_cmd = [
        python_exe,
        "-m",
        "pip",
        "install",
        "--user",
        "-e",
        str(project_root),
    ]

    print(f"Installing DEILE from: {project_root}")
    print(f"Running: {' '.join(base_install_cmd)}")

    try:
        result = subprocess.run(
            base_install_cmd,
            text=True,
            capture_output=True,
            check=False,
        )
    except Exception as exc:
        print(f"ERROR: failed to run pip: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    stderr = result.stderr.strip() or "(no stderr)"
    is_pep668 = "externally-managed-environment" in stderr or "PEP 668" in stderr
    if result.returncode != 0 and is_pep668:
        fallback_cmd = [
            python_exe,
            "-m",
            "pip",
            "install",
            "--user",
            "--break-system-packages",
            "-e",
            str(project_root),
        ]
        print(
            "Detected externally-managed Python. Retrying with --break-system-packages..."
        )
        print(f"Running: {' '.join(fallback_cmd)}")
        result = subprocess.run(
            fallback_cmd,
            text=True,
            capture_output=True,
            check=False,
        )
        stderr = result.stderr.strip() or "(no stderr)"

    if result.returncode != 0:
        print("ERROR: installation failed.", file=sys.stderr)
        print(stderr, file=sys.stderr)
        if "externally-managed-environment" in stderr or "PEP 668" in stderr:
            print(
                "Tip: this Python is externally managed. "
                "Use pipx (`brew install pipx && pipx install -e .`) if needed.",
                file=sys.stderr,
            )
        return result.returncode or 1

    which_cmd = ["/usr/bin/env", "which", "deile"]
    which_result = subprocess.run(which_cmd, text=True, capture_output=True, check=False)
    deile_path = which_result.stdout.strip() if which_result.returncode == 0 else "not found in PATH"

    print("DEILE installed successfully.")
    print(f"deile path: {deile_path}")
    print("Try: deile --help")
    return 0


# ── command-flag dispatch (issue #126) ──────────────────────────────────────


async def _run_command_flag(
    command_name: str,
    command_args: str,
    requires_provider: bool,
) -> int:
    """Dispatch a single slash command in one-shot mode.

    Bootstraps providers only if *requires_provider* is True; otherwise the
    command runs without any LLM provider and never errors on a missing key.
    Renders the resulting :class:`CommandResult` to stdout and returns an
    exit code (0 success / 1 error).
    """
    from deile.commands.base import CommandContext
    from deile.commands.registry import get_command_registry
    from deile.config.manager import ConfigManager
    from deile.config.settings import get_settings

    settings = get_settings()
    settings.working_directory = Path.cwd()
    config_manager = ConfigManager()
    try:
        config_manager.load_config()
    except Exception:  # noqa: BLE001 — config is best-effort for offline flags
        pass

    agent = None
    if requires_provider:
        # Only spin up the full agent (and require an API key) when the flag
        # genuinely needs an LLM provider. Most --flags don't.
        from deile.core.agent import DeileAgent
        from deile.core.models.bootstrap import bootstrap_providers
        from deile.core.models.router import get_model_router
        from deile.parsers.registry import get_parser_registry
        from deile.tools.registry import get_tool_registry

        model_router = get_model_router()
        registered = bootstrap_providers(router=model_router)
        if not registered:
            print(
                "ERROR: no provider configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
                "DEEPSEEK_API_KEY, or GOOGLE_API_KEY.",
                file=sys.stderr,
            )
            return 1
        agent = DeileAgent(
            model_router=model_router,
            tool_registry=get_tool_registry(),
            parser_registry=get_parser_registry(),
            config_manager=config_manager,
        )
        await agent.initialize()
        registry = agent.command_registry
    else:
        registry = get_command_registry(config_manager)
        if len(registry) == 0:
            registry.auto_discover_builtin_commands()

    context = CommandContext(
        user_input=f"/{command_name} {command_args}".strip(),
        args=command_args,
        session_id="oneshot_cli_flag",
        working_directory=str(settings.working_directory),
    )
    context.config_manager = config_manager
    context.agent = agent

    try:
        result = await registry.execute_command(command_name, context)
    except Exception as exc:  # noqa: BLE001 — last-resort catcher; logged below
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    _print_oneshot_content(result.content)
    return 0 if result.success else 1


def _format_help_with_commands(parser: "argparse.ArgumentParser") -> str:
    """Append the slash-command catalog to argparse's stock --help output.

    Uses the same registry source as ``CommandActions.show_help()`` so the
    list is generated dynamically (no hardcoded count — see issue #126).
    """
    base_help = parser.format_help()
    try:
        from deile.commands.registry import get_command_registry
        registry = get_command_registry()
        if len(registry) == 0:
            registry.auto_discover_builtin_commands()
        commands = sorted(
            registry.get_enabled_commands(),
            key=lambda c: c.name,
        )
    except Exception as exc:  # noqa: BLE001 — help must never crash
        return base_help + f"\n[help: command catalog unavailable: {exc}]\n"

    if not commands:
        return base_help

    name_w = max(len(c.name) for c in commands) + 1
    lines = [
        "",
        "interactive slash commands (also usable inside the REPL):",
    ]
    for cmd in commands:
        cmd_type = "LLM" if cmd.has_prompt_template else "Direct"
        lines.append(
            f"  /{cmd.name:<{name_w}}  [{cmd_type:<6}]  {cmd.description}"
        )
    lines.append("")
    lines.append(
        "Tip: most slash commands also have a CLI flag (run `deile --help` to see "
        "the full flag list above)."
    )
    return base_help + "\n".join(lines) + "\n"


# ── main entry point ────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    """`deile` console_script entry point.

    Returns exit code (0 = success, 1 = error).
    """
    _load_dotenv()
    _silence_genai_shutdown_noise()

    # Ensure deile package is importable
    sys.path.insert(0, str(_PROJECT_ROOT))

    if argv is None:
        argv = sys.argv[1:]

    # No args → interactive
    if not argv:
        import logging
        logging.disable()
        asyncio.run(_DeileCLI().run_interactive())
        return 0

    # Build the parser. We disable argparse's default --help so that we can
    # print our extended help (with the slash-command catalog appended).
    parser = argparse.ArgumentParser(
        prog="deile",
        description=(
            "DEILE — Run interactively (no args), send a single message, "
            "or invoke any slash command via its --flag (issue #126)."
        ),
        add_help=False,
    )
    parser.add_argument(
        "-h", "--help",
        dest="show_help",
        action="store_true",
        help="Show this help message (with full slash command catalog) and exit.",
    )
    parser.add_argument(
        "--model",
        dest="model",
        metavar="PROVIDER:MODEL_ID",
        help="Force a specific model (e.g. deepseek:deepseek-v4-flash).",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install DEILE globally for the current user (`pip install --user -e <repo>`).",
    )

    # Auto-generate one --flag per registered slash command (issue #126).
    flag_specs: list = []
    try:
        from deile.commands.cli_flags import (add_command_flags_to_parser,
                                              build_cli_flag_specs,
                                              find_active_spec, get_arg_value)
        from deile.commands.registry import get_command_registry
        registry = get_command_registry()
        if len(registry) == 0:
            registry.auto_discover_builtin_commands()
        flag_specs = build_cli_flag_specs(registry)
        add_command_flags_to_parser(parser, flag_specs)
    except Exception as exc:  # noqa: BLE001 — never block argparse setup
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "Could not register dynamic command flags: %s", exc
        )

    parser.add_argument(
        "message",
        nargs=argparse.REMAINDER,
        help="Message to send to the agent (quote if it contains shell metacharacters).",
    )
    args = parser.parse_args(argv)

    if getattr(args, "show_help", False):
        sys.stdout.write(_format_help_with_commands(parser))
        return 0

    if args.install:
        return _run_self_install()

    # --debug is a global modifier: enable debug mode in settings BEFORE any
    # one-shot dispatch or interactive run, so it actually has an effect.
    if getattr(args, "debug", False):
        try:
            from deile.config.settings import get_settings
            get_settings().debug_enabled = True
        except Exception:  # noqa: BLE001 — best-effort
            pass

    # Did the user pass any --flag bound to a slash command?
    # find_active_spec already skips modifier flags (cli_dispatch=False),
    # so --debug never triggers a one-shot /debug invocation.
    active_spec = find_active_spec(flag_specs, args) if flag_specs else None

    if active_spec is not None:
        import logging
        logging.disable()
        cmd_args = get_arg_value(active_spec, args)
        return asyncio.run(_run_command_flag(
            command_name=active_spec.command_name,
            command_args=cmd_args,
            requires_provider=active_spec.requires_provider,
        ))

    msg = " ".join(args.message).strip()
    if not msg and not sys.stdin.isatty():
        msg = sys.stdin.read().strip()
    if not msg:
        # --debug (and possibly --model) without a message → fall through
        # to interactive mode. Any flag setup already happened above (e.g.
        # debug toggle), so the REPL inherits it.
        debug_only = getattr(args, "debug", False) or args.model
        if debug_only:
            import logging
            logging.disable()
            asyncio.run(_DeileCLI().run_interactive())
            return 0
        # No message AND no flag — the user got the invocation wrong.
        parser.error("no message provided (pass as positional arg, via stdin, or use a --flag)")

    import logging
    logging.disable()
    return asyncio.run(_run_oneshot(msg, forced_model=args.model))


if __name__ == "__main__":
    sys.exit(main())
