"""CLI flag builder — generates argparse flags from registered slash commands.

This module implements decision #24 (issue #126): slash commands declare
their CLI-flag metadata as class attributes, and the CLI's argparse parser
is generated dynamically by walking the registry. Adding a new flag is
therefore a metadata-only change — no edits to ``cli.py``.

Usage in ``deile/cli.py``:

    from deile.commands.cli_flags import (
        build_cli_flag_specs,
        add_command_flags_to_parser,
    )

    registry = CommandRegistry()
    registry.auto_discover_builtin_commands()
    specs = build_cli_flag_specs(registry)
    add_command_flags_to_parser(parser, specs)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    import argparse

    from .registry import CommandRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CLIFlagSpec:
    """Spec for a single CLI flag → slash command mapping.

    Attributes:
        flag: The primary flag name (e.g. ``"--status"``).
        aliases: Optional list of additional flag aliases (e.g. ``["-s"]``).
        command_name: The slash command name to dispatch (e.g. ``"status"``).
        subcommand: Optional sub-argument forwarded to the slash command
            (e.g. ``"list"`` for ``--model-list`` → ``/model list``).
        takes_arg: Whether the flag expects a value (e.g. ``--export PATH``).
        metavar: argparse metavar for the value when ``takes_arg`` is True.
        help: argparse help text.
        requires_provider: Whether the flag's slash command needs an LLM
            provider configured. Flags with ``requires_provider=False`` may
            run even when no API keys are set.
        dispatch: If ``False`` the CLI registers the flag in argparse but
            does NOT invoke the slash command — the flag behaves as a global
            modifier (e.g. ``--debug`` only toggles ``Settings.debug_enabled``).
    """

    flag: str
    command_name: str
    subcommand: Optional[str] = None
    aliases: Optional[List[str]] = None
    takes_arg: bool = False
    metavar: Optional[str] = None
    help: Optional[str] = None
    requires_provider: bool = False
    dispatch: bool = True

    @property
    def dest(self) -> str:
        """argparse ``dest`` derived from the flag name."""
        return self.flag.lstrip("-").replace("-", "_")


def build_cli_flag_specs(registry: "CommandRegistry") -> List[CLIFlagSpec]:
    """Walk the command registry and produce a list of CLI flag specs.

    Reads three optional class attributes per :class:`SlashCommand`:

      * ``cli_flag``: primary single-flag binding (e.g. ``"--status"``).
      * ``cli_extra_flags``: dict of ``"--sub-flag" -> {subcommand, help,
        takes_arg, metavar, requires_provider}`` used for commands that
        expose multiple sub-flags (e.g. ``ModelCommand``, ``PipelineCommand``).
      * ``cli_flag_aliases``: optional list of short aliases for ``cli_flag``.

    Returns:
        A list of :class:`CLIFlagSpec`, sorted by flag name for deterministic
        ``--help`` output.
    """
    specs: List[CLIFlagSpec] = []
    seen_flags: set = set()

    for command in registry.get_all_commands():
        # 1) Primary cli_flag binding (one flag → one slash command, no subcommand)
        primary = getattr(command, "cli_flag", None)
        if primary:
            if primary in seen_flags:
                logger.warning(
                    "Duplicate CLI flag %s (command /%s) — skipping; first wins",
                    primary,
                    command.name,
                )
            else:
                seen_flags.add(primary)
                specs.append(CLIFlagSpec(
                    flag=primary,
                    command_name=command.name,
                    aliases=list(getattr(command, "cli_flag_aliases", None) or []) or None,
                    takes_arg=bool(getattr(command, "cli_takes_arg", False)),
                    metavar=getattr(command, "cli_arg_metavar", None),
                    help=getattr(command, "cli_help", None) or command.description,
                    subcommand=getattr(command, "cli_subcommand", None),
                    requires_provider=bool(getattr(command, "cli_requires_provider", False)),
                    dispatch=bool(getattr(command, "cli_dispatch", True)),
                ))

        # 2) cli_extra_flags: multiple sub-flags fanning out to one slash command
        extra: Dict[str, Dict[str, Any]] = getattr(command, "cli_extra_flags", None) or {}
        for sub_flag, meta in extra.items():
            if sub_flag in seen_flags:
                logger.warning(
                    "Duplicate CLI flag %s (command /%s) — skipping; first wins",
                    sub_flag,
                    command.name,
                )
                continue
            seen_flags.add(sub_flag)
            specs.append(CLIFlagSpec(
                flag=sub_flag,
                command_name=command.name,
                subcommand=meta.get("subcommand"),
                takes_arg=bool(meta.get("takes_arg", False)),
                metavar=meta.get("metavar"),
                help=meta.get("help"),
                requires_provider=bool(meta.get("requires_provider", False)),
                dispatch=bool(meta.get("dispatch", True)),
            ))

    # Stable order — alphabetical by flag for deterministic --help.
    specs.sort(key=lambda s: s.flag)
    return specs


def add_command_flags_to_parser(
    parser: "argparse.ArgumentParser",
    specs: List[CLIFlagSpec],
) -> None:
    """Add each spec to *parser* as a mutually-disambiguated argument.

    Flags that take an argument become ``str``; flags without a value become
    ``store_true``. ``--debug`` is intentionally NOT skipped here: the CLI
    consumes it as a global modifier *before* dispatching one-shot flags,
    so its presence in argparse remains a valid no-op.
    """
    for spec in specs:
        names = [spec.flag] + (spec.aliases or [])
        kwargs: Dict[str, Any] = {
            "dest": spec.dest,
            "help": spec.help or "",
        }
        if spec.takes_arg:
            kwargs["metavar"] = spec.metavar or "VALUE"
            kwargs["default"] = None
        else:
            kwargs["action"] = "store_true"
            kwargs["default"] = False

        parser.add_argument(*names, **kwargs)


def find_active_spec(
    specs: List[CLIFlagSpec],
    args_namespace: Any,
) -> Optional[CLIFlagSpec]:
    """Return the first dispatchable spec whose argparse value is truthy.

    Modifier flags (``dispatch=False``) are skipped — they are global toggles,
    not one-shot dispatchers. Used by the CLI to decide which command to
    invoke in one-shot mode. Returns ``None`` if no dispatchable flag is set.
    """
    for spec in specs:
        if not spec.dispatch:
            continue
        value = getattr(args_namespace, spec.dest, None)
        if value:  # store_true=True, or non-empty/non-None string
            return spec
    return None


def get_arg_value(spec: CLIFlagSpec, args_namespace: Any) -> str:
    """Build the slash-command argument string for *spec* from argparse output.

    Combines the spec's ``subcommand`` (if any) with the user-supplied value
    (if ``takes_arg=True``) into a single string passed to ``CommandContext.args``.
    """
    parts: List[str] = []
    if spec.subcommand:
        parts.append(spec.subcommand)
    if spec.takes_arg:
        value = getattr(args_namespace, spec.dest, None)
        if value and isinstance(value, str):
            parts.append(value)
    return " ".join(parts)
