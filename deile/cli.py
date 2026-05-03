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
                    from deile.core.models.gemini_provider import GeminiProvider
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

            with self.ui.show_loading("Acordando DEILE..."):
                model_router = get_model_router()
                # _bootstrap_providers is sync — call it directly, no asyncio.to_thread
                registered = self._bootstrap_providers(model_router)
                if not registered:
                    self.ui.display_error(
                        "Nenhum provider configurado.",
                        "Defina ao menos uma variável de ambiente: "
                        "ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, GOOGLE_API_KEY.",
                    )
                    return False

                self.agent = DeileAgent(
                    model_router=model_router,
                    tool_registry=get_tool_registry(),
                    parser_registry=get_parser_registry(),
                    config_manager=self.config_manager,
                )
                await self.agent.initialize()

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
        from deile.ui import UIMessage, MessageType

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


# ── one-shot mode ───────────────────────────────────────────────────────────

async def _run_oneshot(message: str, forced_model: Optional[str] = None) -> int:
    """Single-turn non-interactive. stdout = response.content."""
    from deile.config.manager import ConfigManager
    from deile.config.settings import get_settings
    from deile.core.agent import DeileAgent
    from deile.core.models.router import get_model_router
    from deile.core.models.bootstrap import bootstrap_providers
    from deile.parsers.registry import get_parser_registry
    from deile.tools.registry import get_tool_registry

    settings = get_settings()
    settings.working_directory = Path.cwd()
    config_manager = ConfigManager()
    config_manager.load_config()

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

    session = agent.create_session(
        session_id="oneshot_cli_session",
        working_directory=settings.working_directory,
    )
    if forced_model:
        session.context_data["forced_model"] = forced_model
    else:
        preferred = os.environ.get("DEILE_PREFERRED_MODEL")
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

    print(response.content)
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

    parser = argparse.ArgumentParser(
        prog="deile",
        description="DEILE — Run interactively (no args) or send a single message and exit.",
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
    parser.add_argument(
        "message",
        nargs=argparse.REMAINDER,
        help="Message to send to the agent (quote if it contains shell metacharacters).",
    )
    args = parser.parse_args(argv)

    if args.install:
        return _run_self_install()

    msg = " ".join(args.message).strip()
    if not msg and not sys.stdin.isatty():
        msg = sys.stdin.read().strip()
    if not msg:
        parser.error("no message provided (pass as positional arg or via stdin)")

    import logging
    logging.disable()
    return asyncio.run(_run_oneshot(msg, forced_model=args.model))


if __name__ == "__main__":
    sys.exit(main())
