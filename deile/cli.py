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
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import uuid
import venv as _venv  # noqa: N812 — local alias for testability (patched as deile.cli._venv)
from pathlib import Path
from typing import Optional

from deile.commands._sentinels import (POST_SWITCH_ACTION_KEY,
                                       SWITCH_SESSION_KEY)

# ── package root (where deile/ lives) ───────────────────────────────────────
_PACKAGE_ROOT = Path(__file__).parent.resolve()
_PROJECT_ROOT = _PACKAGE_ROOT.parent  # repo root when editable, same when installed
_ENV_KEY_NAMES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "GOOGLE_API_KEY")

# ── install helpers — module-level constants ─────────────────────────────────
_KNOWN_SHELLS = frozenset({"zsh", "bash", "fish"})
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


def _silence_logging() -> None:
    """Suppress all logging output for one-shot/CLI dispatch paths."""
    import logging
    logging.disable()


def _load_exported_env_vars() -> None:
    """Load env vars from ~/.deile/settings.json env.exports into os.environ.

    Preferred alternative to .env files: variables stored via /env set KEY=VALUE
    are exported here, before provider bootstrap.
    Missing or malformed settings are silently ignored.
    """
    try:
        from deile.config.env_store import load_exported_vars
        load_exported_vars()
    except Exception:
        pass


async def _construct_agent(model_router, config_manager):
    """Build and initialize a :class:`DeileAgent` from a bootstrapped router.

    The caller is responsible for bootstrapping providers, creating sessions
    and arranging UI affordances (spinners, autostart) — this helper only
    centralizes the constructor + ``await agent.initialize()`` so any future
    change to either lands in one place.
    """
    from deile.core.agent import DeileAgent
    from deile.parsers.registry import get_parser_registry
    from deile.tools.registry import get_tool_registry

    agent = DeileAgent(
        model_router=model_router,
        tool_registry=get_tool_registry(),
        parser_registry=get_parser_registry(),
        config_manager=config_manager,
    )
    await agent.initialize()
    return agent


def _bootstrap_with_recovery(bootstrap_fn, *, spinner_factory=None) -> list:
    """Run ``bootstrap_fn`` once; if it registered nothing, prompt the user for
    API keys via the TTY wizard and retry. ``bootstrap_fn`` is a zero-arg
    callable returning the list of registered provider names.

    ``spinner_factory`` (optional) is a zero-arg callable returning a fresh
    context manager (e.g. Rich ``Status``). When provided, the spinner is
    active during each bootstrap attempt but is paused around the
    interactive recovery wizard so ``getpass`` prompts render cleanly.
    """
    if spinner_factory is None:
        registered = bootstrap_fn()
        if not registered and _run_env_recovery():
            registered = bootstrap_fn()
        return registered

    with spinner_factory():
        registered = bootstrap_fn()
    if registered:
        return registered
    if not _run_env_recovery():
        return registered
    with spinner_factory():
        return bootstrap_fn()


def _bootstrap_provider_router_or_print_error():
    """Bootstrap a model router with provider recovery; print stderr error on miss.

    Shared by the two plain-stdio entry points (``_run_oneshot`` and
    ``_run_command_flag``) that print the same byte-identical error and
    return exit code 1 when no provider key is set. The interactive
    ``_DeileCLI.initialize()`` path stays inline because it renders the
    failure via ``ui.display_error`` (PT-BR) and drives ``spinner_factory``.

    Returns the bootstrapped router on success, ``None`` when no provider
    registered after the env-recovery wizard — callers map ``None`` → 1.
    """
    from deile.core.models.bootstrap import bootstrap_providers
    from deile.core.models.router import get_model_router

    model_router = get_model_router()
    registered = _bootstrap_with_recovery(
        lambda: bootstrap_providers(router=model_router)
    )
    if not registered:
        print(
            "ERROR: no provider configured. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, "
            "DEEPSEEK_API_KEY, or GOOGLE_API_KEY.",
            file=sys.stderr,
        )
        return None
    return model_router


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
            if "=" in stripped and not stripped.startswith("#") and stripped.split("=", 1)[0].strip() in _ENV_KEY_NAMES:
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

    print(f"\n  {_GREEN}✓{_RESET}  {sum(1 for v in new_keys.values() if v)} chave(s) salva(s) em {env_path}\n")
    return True


# ── interactive mode ─────────────────────────────────────────────────────────

class _DeileCLI:
    """Thin wrapper that reuses the DEILE agent + UI stack."""

    def __init__(self) -> None:
        self.settings: object = None
        self.agent: object = None
        self.default_session: object = None
        self.ui: object = None
        self.config_manager: object = None

    async def initialize(self) -> bool:
        from deile.config.manager import ConfigManager
        from deile.config.settings import get_settings
        from deile.core.models.bootstrap import bootstrap_providers
        from deile.core.models.router import get_model_router
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
            # Pass a spinner factory so the spinner pauses around the
            # interactive recovery wizard (getpass) but resumes for the retry.
            registered = _bootstrap_with_recovery(
                lambda: bootstrap_providers(router=model_router),
                spinner_factory=lambda: self.ui.show_loading("Acordando DEILE..."),
            )

            if not registered:
                self.ui.display_error(
                    "Nenhum provider configurado.",
                    "Defina ao menos uma variável de ambiente: "
                    "ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, GOOGLE_API_KEY.",
                )
                return False

            with self.ui.show_loading("Finalizando inicialização..."):
                self.agent = await _construct_agent(model_router, self.config_manager)

                # gap #3: autostart the pipeline monitor when DEILE_PIPELINE_AUTOSTART=true
                if self.settings.pipeline_autostart:
                    await _autostart_pipeline(self.agent)

                _cli_session_id = f"cli-{int(time.time())}-{uuid.uuid4().hex[:8]}"
                self.default_session = self.agent.create_session(
                    session_id=_cli_session_id,
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

    _IGNORE_DIRS = frozenset({"__pycache__", ".git", "node_modules", ".venv", "venv", "dist", "build", ".deile"})

    def _get_project_files(self) -> list[str]:
        wd = Path(self.settings.working_directory)
        files = [
            str(path.relative_to(wd)).replace("\\", "/")
            for path in wd.rglob("*")
            if path.is_file() and not any(d in path.parts for d in self._IGNORE_DIRS)
        ]
        return sorted(files)[:500]

    # Sentinel returned by get_user_input() when the user presses ESC ESC on
    # an empty prompt — signals the main loop to trigger /rewind.
    _REWIND_SENTINEL = "\x00REWIND\x00"
    # Context-data keys written by /fork, /rewind, /resume to request a session
    # switch without tight-coupling between the command and the CLI class.
    # Sourced from deile.commands._sentinels — single source of truth shared
    # with the commands that write them. _POST_SWITCH_ACTION_KEY carries the
    # follow-up UI work: ``"welcome"`` re-renders the entry banner (used by
    # /clear); ``"replay"`` clears the screen and re-renders the loaded
    # conversation (used by /resume).
    _SWITCH_SESSION_KEY = SWITCH_SESSION_KEY
    _POST_SWITCH_ACTION_KEY = POST_SWITCH_ACTION_KEY

    def _persist_session(self, user_input: str) -> None:
        from .cli_session_helpers import persist_session
        persist_session(self.default_session, user_input)

    def _rollback_history(self, baseline_len: int) -> None:
        from .cli_session_helpers import rollback_history
        rollback_history(self.default_session, baseline_len)

    def _check_session_switch(self) -> None:
        from .cli_session_helpers import check_session_switch
        new_session = check_session_switch(self.default_session, self.agent, self.ui)
        if new_session is not None:
            self.default_session = new_session

    def _replay_history(self, history: list) -> None:
        from .cli_session_helpers import replay_history
        replay_history(self.ui, self.default_session, history)

    async def _stream_with_esc_cancel(self, event_stream) -> bool:
        """Run display_streaming_turn, cancelling on ESC keypress.

        Returns ``True`` if the stream was cancelled by ESC, ``False``
        otherwise (clean completion, no-TTY fallback, or platform without
        termios).  Callers use the return value to roll back any history
        entries that ``process_input_stream`` added before the cancellation.

        Uses a daemon thread to watch for a plain ESC byte (0x1b without a
        following escape-sequence) while stdin is briefly set to cbreak mode.
        Falls back to the plain streaming call when stdin is not a TTY or when
        the platform is Windows (no termios).

        IMPORTANT: this watcher *consumes bytes* from stdin (cbreak mode +
        ``sys.stdin.read(1)``) — including arrow keys and ESC inside any
        sub-prompt that opens during streaming.  Callers must NEVER invoke
        this for paths that may open an interactive ``prompt_toolkit``
        sub-prompt (slash commands like ``/rewind``, ``/resume``), or the
        sub-prompt's input is silently eaten by this thread and the UI
        appears frozen until Ctrl+C.  Use :meth:`UIManager.display_streaming_turn`
        directly for those cases.
        """
        try:
            import select as _select
            import termios
            import tty
        except ImportError:
            await self.ui.display_streaming_turn(event_stream)
            return False

        if not sys.stdin.isatty():
            await self.ui.display_streaming_turn(event_stream)
            return False

        esc_event: asyncio.Event = asyncio.Event()
        watcher_done = threading.Event()
        loop = asyncio.get_running_loop()

        try:
            saved = termios.tcgetattr(sys.stdin.fileno())
        except Exception:
            await self.ui.display_streaming_turn(event_stream)
            return False

        def _watch() -> None:
            try:
                tty.setcbreak(sys.stdin.fileno())
                while not esc_event.is_set():
                    r, _, _ = _select.select([sys.stdin], [], [], 0.1)
                    if not r:
                        continue
                    ch = sys.stdin.read(1)
                    if ch != "\x1b":
                        continue
                    # Distinguish plain ESC from multi-byte escape sequences
                    # (arrow keys etc.) by checking for more bytes within 50ms.
                    r2, _, _ = _select.select([sys.stdin], [], [], 0.05)
                    if not r2:
                        loop.call_soon_threadsafe(esc_event.set)
                        break
                    # Escape sequence — consume and ignore remaining bytes.
                    while _select.select([sys.stdin], [], [], 0.01)[0]:
                        sys.stdin.read(1)
            except Exception:
                pass
            finally:
                try:
                    termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
                except Exception:
                    pass
                watcher_done.set()

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()

        stream_task = asyncio.ensure_future(self.ui.display_streaming_turn(event_stream))
        esc_task = asyncio.ensure_future(esc_event.wait())
        try:
            done, pending = await asyncio.wait(
                [stream_task, esc_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass  # expected — we requested this cancellation
                except Exception:
                    pass

            if esc_event.is_set() and stream_task not in done:
                self.ui.console.print("\n[yellow](ESC — request cancelada)[/yellow]")
                return True

            if stream_task in done and not stream_task.cancelled():
                stream_task.result()  # surface any exception
                return False
        finally:
            # Stop the watcher and wait briefly for it to release stdin to
            # avoid a race where the next ``get_user_input`` runs while
            # stdin is still in cbreak mode.
            esc_event.set()
            if not watcher_done.is_set():
                await asyncio.to_thread(watcher_done.wait, 0.3)
            try:
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)
            except Exception:
                pass
        return False

    async def run_interactive(self) -> None:
        from rich.panel import Panel
        from rich.text import Text

        from deile.ui import MessageType, UIMessage

        if not await self.initialize():
            return

        self.ui.show_welcome(self.default_session)

        try:
            while True:
                user_input = await self.ui.get_user_input("\n > ")
                user_input = user_input.strip()

                # ESC ESC on empty prompt → trigger /rewind
                if user_input == self._REWIND_SENTINEL:
                    user_input = "/rewind"
                elif not user_input:
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
                is_slash = user_input.startswith("/")

                # Slash commands must NOT run on the streaming path:
                #   1. The ESC-cancel watcher (cbreak + read(1)) eats escape
                #      sequences from sub-prompts like /rewind's and /resume's
                #      selectors, freezing them.
                #   2. The streaming renderer's Rich ``Live`` region + 100ms
                #      spinner task fights for cursor position with the
                #      prompt_toolkit ``Application`` opened by the selector.
                # Slash commands don't truly stream (the agent emits a single
                # aggregated event at the end), so the non-streaming path is
                # both safer and visually equivalent.
                if streaming and not is_slash:
                    # Snapshot history length BEFORE the agent appends the
                    # user message (it does so as the first action of
                    # ``process_input_stream``). On cancel — ESC or
                    # KeyboardInterrupt — we truncate back to this length to
                    # avoid leaving an orphan ``user`` entry that would
                    # poison the next turn (provider would merge it with
                    # the new user message, producing the "/" echo bug).
                    baseline_len = len(self.default_session.conversation_history)
                    event_stream = self.agent.process_input_stream(
                        user_input=user_input,
                        session_id=self.default_session.session_id,
                    )
                    cancelled = False
                    try:
                        cancelled = await self._stream_with_esc_cancel(event_stream)
                    except KeyboardInterrupt:
                        self.ui.console.print("\n[yellow](turn interrupted)[/yellow]")
                        cancelled = True
                    if cancelled:
                        self._rollback_history(baseline_len)
                    self._persist_session(user_input)
                    self._check_session_switch()
                    continue

                # Slash commands run WITHOUT the loading spinner: Rich's
                # ``Status`` uses an auto-refreshing ``Live`` thread that
                # also conflicts with any sub-prompt the command may open
                # (e.g. /rewind, /resume, /model use selectors). They are
                # fast or open their own UI, so no spinner is needed.
                if is_slash:
                    response = await self.agent.process_input(
                        user_input=user_input,
                        session_id=self.default_session.session_id,
                    )
                else:
                    with self.ui.show_loading("Processando sua solicitação..."):
                        response = await self.agent.process_input(
                            user_input=user_input,
                            session_id=self.default_session.session_id,
                        )

                self._persist_session(user_input)
                self._check_session_switch()

                meta = response.metadata or {}
                if meta.get("suppress_response_display"):
                    pass  # command renders its own UI via post-switch action
                elif meta.get("budget_exceeded"):
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


# ── pipeline autostart helper ────────────────────────────────────────────────


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


# ── one-shot mode ────────────────────────────────────────────────────────────


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
    items = content if isinstance(content, list) else [content]
    for item in items:
        console.print(item)


async def _run_oneshot(message: str, forced_model: Optional[str] = None) -> int:
    """Single-turn non-interactive. stdout = response.content."""
    from deile.config.manager import ConfigManager
    from deile.config.settings import get_settings

    settings = get_settings()
    settings.working_directory = Path.cwd()
    config_manager = ConfigManager()
    config_manager.load_config()

    model_router = _bootstrap_provider_router_or_print_error()
    if model_router is None:
        return 1

    agent = await _construct_agent(model_router, config_manager)

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


# ── install helpers ──────────────────────────────────────────────────────────


def _user_scripts_dir() -> Path:
    """Return the directory where `pip install --user` places console scripts.

    Picks the right sysconfig scheme per platform:
      - Linux:        posix_user           → ~/.local/bin
      - macOS (framework Python):
                      osx_framework_user   → ~/Library/Python/X.Y/bin
      - Windows:      nt_user              → %APPDATA%\\Python\\PythonXY\\Scripts
    """
    if hasattr(sysconfig, "get_preferred_scheme"):
        scheme = sysconfig.get_preferred_scheme("user")
    elif os.name == "nt":
        scheme = "nt_user"
    elif sys.platform == "darwin" and getattr(sys, "_framework", ""):
        scheme = "osx_framework_user"
    else:
        scheme = "posix_user"
    return Path(sysconfig.get_path("scripts", scheme=scheme))


def _wrapper_target_dir() -> Path:
    """Pick the directory where the global `deile` wrapper should land.

    On POSIX, prefer ~/.local/bin (commonly already on PATH). On Windows or if
    ~/.local/bin is unusable, fall back to the sysconfig user-scripts dir.
    """
    if os.name == "nt":
        return _user_scripts_dir()
    return Path.home() / ".local" / "bin"


async def _pip_run(*args: str, step: str, sanitized_path: Optional[str] = None) -> None:
    """Run a pip sub-command; raise DEILEInstallError on non-zero exit."""
    from deile.core.exceptions import DEILEInstallError

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        stderr_text = (stderr or b"").decode("utf-8", errors="replace")[:500]
        raise DEILEInstallError(
            f"pip {step} failed (rc={proc.returncode}): {stderr_text}",
            step=step,
            sanitized_path=sanitized_path,
        )


async def _create_venv_with_deile(venv_dir: Path, repo_root: Path, mode_label: str) -> Path:
    """Ensure an isolated venv at ``venv_dir`` has DEILE + deps installed.

    Steps (idempotent):
      1. Create the venv if missing.
      2. Upgrade pip.
      3. Install ``requirements.txt`` (frozen versions — same set the bootstrap uses).
      4. Register the deile package editable with ``--no-deps`` so the
         ``deile`` console script is created without disturbing pinned deps.

    Returns the absolute path to ``<venv>/bin/deile`` (or ``Scripts\\deile.exe``).
    """
    from deile.core.exceptions import DEILEInstallError

    # Canonicalize paths to prevent injection / traversal (Pilar 08)
    try:
        venv_dir = venv_dir.resolve()
        repo_root = repo_root.resolve()
    except OSError as exc:
        raise DEILEInstallError(
            f"failed to resolve path: {exc}",
            step="resolve_paths",
            sanitized_path=venv_dir.name,
        ) from exc

    # Safety: ensure venv_dir is within a reasonable location
    home = Path.home().resolve()
    if not (str(venv_dir).startswith(str(repo_root)) or str(venv_dir).startswith(str(home))):
        raise DEILEInstallError(
            "venv_dir is outside allowed locations (must be under repo or home)",
            step="validate_venv_path",
        )

    venv_py = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    try:
        if not venv_py.exists():
            print(f"[{mode_label}] Creating venv…")
            venv_dir.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                _venv.EnvBuilder(with_pip=True).create, str(venv_dir)
            )
        else:
            print(f"[{mode_label}] Reusing existing venv")

        print(f"[{mode_label}] Upgrading pip…")
        await _pip_run(
            str(venv_py), "-m", "pip", "install",
            "--disable-pip-version-check", "-q", "--upgrade", "pip",
            step="upgrade_pip", sanitized_path=venv_dir.name,
        )

        requirements = repo_root / "requirements.txt"
        if requirements.exists():
            print(f"[{mode_label}] Installing dependencies from {requirements.name}…")
            await _pip_run(
                str(venv_py), "-m", "pip", "install",
                "--disable-pip-version-check", "-r", str(requirements),
                step="install_deps", sanitized_path=requirements.name,
            )
        else:
            print(f"[{mode_label}] WARNING: no requirements.txt — skipping dep install")

        print(f"[{mode_label}] Registering DEILE entry script (editable, no-deps)…")
        await _pip_run(
            str(venv_py), "-m", "pip", "install",
            "--disable-pip-version-check", "-q", "--no-deps", "-e", str(repo_root),
            step="install_editable",
        )

        deile_script = venv_dir / ("Scripts/deile.exe" if os.name == "nt" else "bin/deile")
        if not deile_script.exists():
            raise DEILEInstallError(
                "console script not created",
                step="verify_script",
                sanitized_path=deile_script.name,
            )
        return deile_script

    except DEILEInstallError:
        raise
    except Exception as exc:
        raise DEILEInstallError(
            f"venv creation failed: {exc}",
            step="create_venv",
            sanitized_path=venv_dir.name,
        ) from exc


def _link_global_command(target_dir: Path, source_script: Path, *, force: bool = False) -> Path:
    """Create the user-facing `deile` shim that points at ``source_script``.

    On POSIX: a symlink at ``target_dir/deile``.
    On Windows: a ``.cmd`` shim that execs the source script.

    If a file/symlink already exists at the target, we ask before replacing
    (or replace silently when ``force=True``).
    """
    from deile.core.exceptions import DEILEInstallError

    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ("deile.cmd" if os.name == "nt" else "deile")

    if target.exists() or target.is_symlink():
        existing = "symlink" if target.is_symlink() else "regular file"
        if not force:
            try:
                ans = input(f"  {target.name} already exists ({existing}). Replace? [Y/n]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                ans = "n"
            if ans not in ("", "y", "yes"):
                raise DEILEInstallError(
                    "refusing to overwrite existing shim",
                    step="link_command",
                    sanitized_path=target.name,
                )
        try:
            target.unlink()
        except OSError as exc:
            raise DEILEInstallError(
                f"could not remove existing shim: {exc}",
                step="unlink_old_shim",
                sanitized_path=target.name,
            ) from exc

    if os.name == "nt":
        target.write_text(f'@echo off\r\n"{source_script}" %*\r\n', encoding="utf-8")
    else:
        try:
            target.symlink_to(source_script)
        except OSError as exc:
            raise DEILEInstallError(
                f"could not create symlink: {exc}",
                step="create_symlink",
                sanitized_path=target.name,
            ) from exc
    return target


def _ensure_scripts_dir_on_path(scripts_dir: Path) -> tuple[bool, Optional[Path], str]:
    """Append ``export PATH=...`` to the user's shell rc file if not already there.

    Returns (modified, rc_path, fallback_hint):
      modified=True            → rc file was edited.
      modified=False, rc set, hint==""  → already configured in rc; just needs reload.
      modified=False, rc=None  → could not auto-edit (Windows / unknown shell);
                                 caller should print fallback_hint.
      modified=False, rc set, hint!=""  → tried but failed (perm/IO); print hint.

    Security (Pilar 08):
      - The rc file path is resolved to canonical form (no symlink traversal).
      - Writing is atomic: tempfile in same directory, then os.replace().
      - The check for already-configured is line-by-line (not substring match).

    """
    from deile.core.exceptions import DEILEInstallError

    # double-quote would break `export PATH="..."` syntax; newline would split the rc line.
    scripts_dir_str = str(scripts_dir)
    if '"' in scripts_dir_str or "\n" in scripts_dir_str:
        return (
            False, None,
            f"Path contains unsupported characters for auto-configuration.\n"
            f"Add manually:\n    export PATH=\"{scripts_dir_str}:$PATH\""
        )

    export_line_posix = f'export PATH="{scripts_dir}:$PATH"'

    if os.name == "nt":
        hint = (
            f'PowerShell: $env:Path = "{scripts_dir};$env:Path"\n'
            "  (persist via System Properties → Environment Variables)"
        )
        return (False, None, hint)

    shell = os.path.basename(os.environ.get("SHELL", ""))
    if shell not in _KNOWN_SHELLS:
        return (False, None, f'Add to your shell rc:\n    {export_line_posix}')

    home = Path.home().resolve()  # canonical home

    if shell == "zsh":
        rc = (home / ".zshrc").resolve()
        export_line = export_line_posix
    elif shell == "bash":
        rc = (home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")).resolve()
        export_line = export_line_posix
    else:  # fish
        rc = (home / ".config" / "fish" / "config.fish").resolve()
        export_line = f'set -gx PATH "{scripts_dir}" $PATH'

    # Safety: rc file must be within $HOME (prevent symlink traversal out of home)
    if not str(rc).startswith(str(home)):
        raise DEILEInstallError(
            "rc file resolves outside of home directory",
            step="validate_rc_path",
            sanitized_path=rc.name,
        )

    try:
        existing = rc.read_text(encoding="utf-8") if rc.exists() else ""
    except OSError as exc:
        return (False, rc, f"Could not read {rc.name}: {exc}.\nAdd manually:\n    {export_line}")

    # ── Idempotency: line-by-line check (not substring) ──
    for line in existing.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue  # skip commented-out lines
        if scripts_dir_str in stripped and "PATH" in stripped.upper():
            return (False, rc, "")  # already configured

    marker = "# Added by `deile --install` — places the `deile` command on PATH\n"
    new_content = (existing.rstrip("\n") + "\n\n" if existing else "") + marker + export_line + "\n"

    # ── Atomic write: tempfile in same directory, then os.replace ──
    try:
        rc.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(rc.parent), prefix=".deile_rc_", suffix=".tmp")
        try:
            os.write(fd, new_content.encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp_path, str(rc))  # atomic on POSIX
    except OSError as exc:
        raise DEILEInstallError(
            f"Could not write {rc.name}: {exc}",
            step="write_rc_file",
            sanitized_path=rc.name,
        ) from exc

    return (True, rc, "")


def _prompt_install_mode() -> Optional[str]:
    """Interactively pick install mode. Returns 'global', 'local', or None."""
    print()
    print("Install mode:")
    print()
    print("  [g] Global  — isolated venv at ~/.deile/venv/")
    print("                Recommended. DEILE deps don't touch your system or user-site Python.")
    print("                Works no matter where you cd to.")
    print()
    print("  [l] Local   — uses this repo's .venv/")
    print("                The `deile` command points at this specific clone. If you")
    print("                move or delete this directory, the command stops working.")
    print()
    print("  [q] Quit")
    print()
    while True:
        try:
            choice = input("Choice [g/l/q] (default g): ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return None
        if choice in ("", "g", "global"):
            return "global"
        if choice in ("l", "local"):
            return "local"
        if choice in ("q", "quit", "exit"):
            return None
        print(f"Unrecognized choice: {choice!r}. Please answer g, l, or q.")


async def _run_self_install_async(mode: Optional[str] = None) -> int:
    """Install DEILE so `deile` is reachable from any working directory.

    Two modes (interactive prompt unless ``mode`` is provided):

      global → creates an isolated venv at ~/.deile/venv/ that is dedicated
               to DEILE. Your system / user-site Python is untouched.

      local  → uses <repo>/.venv/ (created on the fly if missing). The
               `deile` command is bound to *this* clone of the repo.

    Both modes drop a thin shim at ~/.local/bin/deile (POSIX) or
    %USERPROFILE%/.../Scripts/deile.cmd (Windows) so the command works from
    any directory without polluting site-packages.
    """
    from deile.core.exceptions import DEILEInstallError

    repo_root = _PROJECT_ROOT

    if mode is None:
        mode = _prompt_install_mode()
        if mode is None:
            print("Cancelled.")
            return 1

    if mode == "global":
        venv_dir = Path.home() / ".deile" / "venv"
    elif mode == "local":
        venv_dir = repo_root / ".venv"
    else:
        print(f"ERROR: unknown install mode {mode!r} (expected 'global' or 'local').", file=sys.stderr)
        return 2

    print()
    print(f"Installing DEILE — mode: {mode}")
    print(f"  repo: {repo_root}")

    created_venv: Optional[Path] = None

    try:
        deile_script = await _create_venv_with_deile(venv_dir, repo_root, mode)
        # Capture resolved path for rollback; success guarantees venv exists.
        created_venv = deile_script.parent.parent
    except DEILEInstallError as exc:
        detail = exc.sanitized_path or "unknown"
        print(f"ERROR: {exc.message} ({detail})", file=sys.stderr)
        return 1

    target_dir = _wrapper_target_dir()
    try:
        wrapper = _link_global_command(target_dir, deile_script)
    except DEILEInstallError as exc:
        # Roll back the venv to avoid a partially-installed state (Princípio 9).
        if created_venv is not None:
            shutil.rmtree(created_venv, ignore_errors=True)
        detail = exc.sanitized_path or "unknown"
        print(f"ERROR: {exc.message} ({detail}) — rolled back venv.", file=sys.stderr)
        return 1

    print()
    print("DEILE installed successfully.")
    print(f"  shim:   {wrapper.name} (in {wrapper.parent})")
    print("  target: venv")

    if os.name != "nt":
        which = await asyncio.to_thread(
            subprocess.run,
            ["/usr/bin/env", "which", "deile"],
            text=True, capture_output=True, check=False,
        )
        if which.returncode == 0:
            found_path = Path(which.stdout.strip())
            try:
                on_path_now = found_path.resolve() == wrapper.resolve()
            except OSError:
                on_path_now = False
            if not on_path_now and found_path.exists():
                print(
                    f"Note: `which deile` returned {found_path.name!r} which does not "
                    "point to the newly installed wrapper. You may have a stale binary."
                )
        else:
            on_path_now = False
    else:
        on_path_now = False

    if on_path_now:
        print()
        print("Try: deile --help")
        return 0

    print()
    try:
        modified, rc_path, hint = _ensure_scripts_dir_on_path(target_dir)
    except DEILEInstallError as exc:
        print(f"Note: could not auto-configure PATH ({exc.message}).")
        print(f"Add {target_dir.name} to your PATH manually.")
        return 0

    if modified:
        print(f"Added PATH export to {rc_path.name}.")
        print(f"Run:  source {rc_path}   (or open a new terminal)")
        print("Then: deile --help")
    elif rc_path is not None and not hint:
        print(f"PATH already configured in {rc_path.name}, but not in current shell session.")
        print(f"Run:  source {rc_path}   (or open a new terminal)")
        print("Then: deile --help")
    else:
        print(hint)
    return 0


def _run_self_install(mode: Optional[str] = None) -> int:
    """Synchronous wrapper around _run_self_install_async for CLI entry point."""
    return asyncio.run(_run_self_install_async(mode))


# ── command-flag dispatch (issue #126) ───────────────────────────────────────


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
        model_router = _bootstrap_provider_router_or_print_error()
        if model_router is None:
            return 1
        agent = await _construct_agent(model_router, config_manager)
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

    Uses the same registry source as ``HelpCommand.execute()`` so the
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
    cmd_lines = [
        f"  /{c.name:<{name_w}}  [{'LLM' if c.has_prompt_template else 'Direct':<6}]  {c.description}"
        for c in commands
    ]
    lines = [
        "",
        "interactive slash commands (also usable inside the REPL):",
        *cmd_lines,
        "",
        "Tip: most slash commands also have a CLI flag (run `deile --help` to see the full flag list above).",
    ]
    return base_help + "\n".join(lines) + "\n"


# ── main entry point ─────────────────────────────────────────────────────────

def main(argv: Optional[list[str]] = None) -> int:
    """`deile` console_script entry point.

    Returns exit code (0 = success, 1 = error).
    """
    _load_dotenv()
    _load_exported_env_vars()
    _silence_genai_shutdown_noise()

    # Ensure deile package is importable
    sys.path.insert(0, str(_PROJECT_ROOT))

    if argv is None:
        argv = sys.argv[1:]

    # No args → interactive
    if not argv:
        _silence_logging()
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
        help="Install DEILE so `deile` is reachable from any directory. Prompts for global "
             "(isolated venv at ~/.deile/venv/) or local (uses <repo>/.venv/). Use --install-mode "
             "to skip the prompt.",
    )
    parser.add_argument(
        "--install-mode",
        choices=("global", "local"),
        default=None,
        help="Non-interactive install target for --install. 'global' = isolated venv at "
             "~/.deile/venv/. 'local' = <repo>/.venv/. Both write a shim to ~/.local/bin/deile.",
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

    if args.install_mode and not args.install:
        print("ERROR: --install-mode requires --install.", file=sys.stderr)
        return 2
    if args.install:
        return _run_self_install(mode=args.install_mode)

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
        _silence_logging()
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
        # --debug or --model without a message → fall through to interactive mode.
        if getattr(args, "debug", False) or args.model:
            _silence_logging()
            asyncio.run(_DeileCLI().run_interactive())
            return 0
        parser.error("no message provided (pass as positional arg, via stdin, or use a --flag)")

    _silence_logging()
    return asyncio.run(_run_oneshot(msg, forced_model=args.model))


if __name__ == "__main__":
    sys.exit(main())
