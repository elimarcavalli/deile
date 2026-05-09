"""Tests for CLI flag wiring (issue #126).

Validates:
  • Every slash command that declares ``cli_flag`` produces an argparse flag.
  • Flag dispatch invokes the correct slash command via the registry.
  • ``--help`` lists every registered builtin command (count is dynamic).
  • Flags that don't need an LLM provider run without API keys.
  • ``--export <path>`` accepts a positional value.
  • ``--model-strategy <name>`` accepts a positional value.
  • Unknown flags fail with a non-zero exit code.
  • The base :class:`SlashCommand` exposes the metadata fields the CLI relies on.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stderr, redirect_stdout
from typing import List
from unittest.mock import patch

import pytest

from deile.cli import main as cli_main
from deile.commands.cli_flags import (CLIFlagSpec, add_command_flags_to_parser,
                                      build_cli_flag_specs, find_active_spec,
                                      get_arg_value)
from deile.commands.registry import CommandRegistry

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_registry() -> CommandRegistry:
    r = CommandRegistry()
    r.auto_discover_builtin_commands()
    return r


def _purge_registry_singleton() -> None:
    """Reset the module-level singleton so tests are deterministic."""
    import deile.commands.registry as reg
    reg._command_registry = None


def _run_cli(argv: List[str]) -> tuple[int, str, str]:
    """Run ``cli.main(argv)`` with isolated env, capturing stdout/stderr."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli_main(argv)
        except SystemExit as exc:
            code = int(exc.code) if exc.code is not None else 0
    return code, out.getvalue(), err.getvalue()


_PROVIDER_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
)


@pytest.fixture
def no_api_keys(monkeypatch):
    """Strip every provider API key so read-only flags exercise the no-bootstrap path."""
    for k in _PROVIDER_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    _purge_registry_singleton()
    return monkeypatch


# ---------------------------------------------------------------------------
# 1. metadata fields exist on SlashCommand base
# ---------------------------------------------------------------------------


class TestSlashCommandMetadata:
    """The CLI flag attributes documented in 04-MODELO-COMPONENTES.md exist."""

    def test_base_has_cli_flag_attribute(self):
        from deile.commands.base import SlashCommand
        assert hasattr(SlashCommand, "cli_flag")
        assert SlashCommand.cli_flag is None

    def test_base_has_cli_takes_arg_attribute(self):
        from deile.commands.base import SlashCommand
        assert hasattr(SlashCommand, "cli_takes_arg")
        assert SlashCommand.cli_takes_arg is False

    def test_base_has_cli_arg_metavar_attribute(self):
        from deile.commands.base import SlashCommand
        assert hasattr(SlashCommand, "cli_arg_metavar")
        assert SlashCommand.cli_arg_metavar is None

    def test_base_has_cli_help_attribute(self):
        from deile.commands.base import SlashCommand
        assert hasattr(SlashCommand, "cli_help")

    def test_base_has_cli_requires_provider_attribute(self):
        from deile.commands.base import SlashCommand
        assert hasattr(SlashCommand, "cli_requires_provider")
        assert SlashCommand.cli_requires_provider is False


# ---------------------------------------------------------------------------
# 2. build_cli_flag_specs reads from registry (no hardcoding in cli.py)
# ---------------------------------------------------------------------------


class TestBuildSpecsFromRegistry:
    def test_status_spec_present(self):
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        flags = {s.flag for s in specs}
        assert "--status" in flags

    def test_specs_dispatch_to_correct_command_name(self):
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        by_flag = {s.flag: s for s in specs}
        assert by_flag["--status"].command_name == "status"
        assert by_flag["--cost"].command_name == "cost"
        assert by_flag["--memory"].command_name == "memory"
        assert by_flag["--version"].command_name == "version"

    def test_extra_flags_set_subcommand(self):
        """ModelCommand exposes 4 sub-flags; pipeline exposes 3."""
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        by_flag = {s.flag: s for s in specs}
        assert by_flag["--model-list"].subcommand == "list"
        assert by_flag["--model-current"].subcommand == "current"
        assert by_flag["--model-strategy"].subcommand == "strategy"
        assert by_flag["--model-budget"].subcommand == "budget"
        assert by_flag["--pipeline-status"].subcommand == "status"
        assert by_flag["--pipeline-start"].subcommand == "start"
        assert by_flag["--pipeline-stop"].subcommand == "stop"

    def test_export_takes_arg(self):
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        by_flag = {s.flag: s for s in specs}
        assert by_flag["--export"].takes_arg is True
        assert by_flag["--export"].metavar == "CAMINHO"

    def test_model_strategy_takes_arg(self):
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        by_flag = {s.flag: s for s in specs}
        assert by_flag["--model-strategy"].takes_arg is True

    def test_specs_are_sorted(self):
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        flags = [s.flag for s in specs]
        assert flags == sorted(flags), "Specs should be sorted for deterministic --help"

    def test_19_dispatchable_flags_minimum(self):
        """Issue #126 promises 19 new flags. cli_flag + cli_extra_flags."""
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        # Issue #126: 11 + 8 = 19 dispatchable flags. We may also have other
        # cli_flag declarations slip in (--debug is one of them) — but the
        # 19 listed in the issue MUST all be present.
        expected = {
            # 11 high-priority
            "--status", "--config", "--debug", "--cost", "--tools",
            "--memory", "--skills", "--model-current", "--model-list",
            "--version",  # --help is argparse-builtin, not in specs
            # 8 medium-priority
            "--pipeline-status", "--pipeline-start", "--pipeline-stop",
            "--logs", "--export", "--clear", "--model-strategy",
            "--model-budget",
        }
        actual = {s.flag for s in specs}
        missing = expected - actual
        assert not missing, f"Missing flags from issue #126: {missing}"


# ---------------------------------------------------------------------------
# 3. argparse integration — every spec becomes a flag
# ---------------------------------------------------------------------------


class TestArgparseIntegration:
    def test_add_command_flags_to_parser_round_trip(self):
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        parser = argparse.ArgumentParser(add_help=False)
        add_command_flags_to_parser(parser, specs)
        # Every spec must be parseable
        for spec in specs:
            if spec.takes_arg:
                args = parser.parse_args([spec.flag, "VALUE"])
                assert getattr(args, spec.dest) == "VALUE"
            else:
                args = parser.parse_args([spec.flag])
                assert getattr(args, spec.dest) is True

    def test_find_active_spec_returns_first_truthy(self):
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        parser = argparse.ArgumentParser(add_help=False)
        add_command_flags_to_parser(parser, specs)
        args = parser.parse_args(["--status"])
        active = find_active_spec(specs, args)
        assert active is not None
        assert active.flag == "--status"

    def test_find_active_spec_skips_modifier_flags(self):
        """Modifier flags (``dispatch=False``) must NOT be selected for one-shot dispatch."""
        modifier = CLIFlagSpec(
            flag="--noop-modifier", command_name="debug", dispatch=False,
        )
        dispatchable = CLIFlagSpec(
            flag="--noop-dispatch", command_name="status",
        )
        ns = argparse.Namespace(
            noop_modifier=True, noop_dispatch=True,
        )
        active = find_active_spec([modifier, dispatchable], ns)
        assert active is dispatchable, "modifier flag must be skipped"

    def test_find_active_spec_returns_none_when_no_flag(self):
        registry = _make_registry()
        specs = build_cli_flag_specs(registry)
        parser = argparse.ArgumentParser(add_help=False)
        add_command_flags_to_parser(parser, specs)
        args = parser.parse_args([])
        assert find_active_spec(specs, args) is None

    def test_get_arg_value_combines_subcommand_and_value(self):
        spec = CLIFlagSpec(
            flag="--export", command_name="export",
            takes_arg=True, metavar="PATH",
        )
        ns = argparse.Namespace(export="/tmp/foo")
        assert get_arg_value(spec, ns) == "/tmp/foo"

    def test_get_arg_value_subcommand_only(self):
        spec = CLIFlagSpec(
            flag="--model-list", command_name="model", subcommand="list",
        )
        ns = argparse.Namespace(model_list=True)
        assert get_arg_value(spec, ns) == "list"


# ---------------------------------------------------------------------------
# 4. /help lists EVERY enabled command (dynamic count, no hardcoding)
# ---------------------------------------------------------------------------


class TestHelpListsEveryCommand:
    def test_help_lists_all_registered_commands(self):
        _purge_registry_singleton()
        code, stdout, _ = _run_cli(["--help"])
        assert code == 0
        # Every enabled command in the registry must appear in --help output.
        registry = _make_registry()
        for cmd in registry.get_enabled_commands():
            assert f"/{cmd.name}" in stdout, (
                f"--help is missing /{cmd.name} — registry has it but "
                "_format_help_with_commands didn't render it."
            )

    def test_help_count_matches_registry(self):
        _purge_registry_singleton()
        code, stdout, _ = _run_cli(["--help"])
        assert code == 0
        registry = _make_registry()
        # Count commands listed in help by counting unique '/cmd ' occurrences.
        # Make this resilient to ordering by checking each name individually.
        listed = sum(
            1 for cmd in registry.get_enabled_commands()
            if f"/{cmd.name}" in stdout
        )
        assert listed == len(registry.get_enabled_commands())


# ---------------------------------------------------------------------------
# 5. Smoke tests — every dispatchable flag invokes a command and exits 0
# ---------------------------------------------------------------------------


# These flags are pure no-side-effect read-only; safe to run in CI.
_SAFE_FLAGS_NO_ARG: List[str] = [
    "--version",
    "--status",
    "--config",
    "--cost",
    "--tools",
    "--memory",
    "--skills",
    "--logs",
    "--clear",
    "--model-current",
    "--model-list",
    "--model-budget",
    "--pipeline-status",
    "--pipeline-stop",
]


@pytest.mark.parametrize("flag", _SAFE_FLAGS_NO_ARG)
class TestFlagSmoke:
    def test_flag_exits_zero_without_api_keys(self, flag, no_api_keys):
        """Read-only flags must work even when no provider API key is set."""
        code, stdout, stderr = _run_cli([flag])
        assert code == 0, (
            f"`deile {flag}` returned exit={code}\n"
            f"stdout={stdout[:500]}\nstderr={stderr[:500]}"
        )


# ---------------------------------------------------------------------------
# 6. flags with values
# ---------------------------------------------------------------------------


class TestFlagsWithArguments:
    def test_export_accepts_path_argument(self, tmp_path, no_api_keys):
        export_target = tmp_path / "export-out"
        code, _stdout, stderr = _run_cli(["--export", str(export_target)])
        assert code == 0, f"stderr={stderr}"

    def test_export_accepts_path_via_equals_syntax(self, tmp_path, no_api_keys):
        """argparse must accept ``--export=PATH`` as well as ``--export PATH``."""
        export_target = tmp_path / "export-equals"
        code, _stdout, stderr = _run_cli([f"--export={export_target}"])
        assert code == 0, f"stderr={stderr}"

    def test_model_strategy_accepts_name_argument(self, no_api_keys):
        code, stdout, stderr = _run_cli(["--model-strategy", "task_optimized"])
        assert code == 0, f"stderr={stderr}"
        assert "Strategy" in stdout or "strategy" in stdout.lower()

    def test_model_strategy_rejects_invalid_name(self, no_api_keys):
        code, _stdout, _stderr = _run_cli(["--model-strategy", "bogus_strategy"])
        assert code != 0


# ---------------------------------------------------------------------------
# 7. error paths
# ---------------------------------------------------------------------------


class TestErrorPaths:
    def test_unknown_flag_exits_nonzero(self):
        _purge_registry_singleton()
        code, _, stderr = _run_cli(["--this-flag-does-not-exist"])
        assert code != 0
        assert "unrecognized" in stderr or "error" in stderr.lower()


# ---------------------------------------------------------------------------
# 7b. registry edge cases
# ---------------------------------------------------------------------------


class TestRegistryEdgeCases:
    def test_empty_registry_yields_empty_specs(self):
        """``build_cli_flag_specs`` on an empty registry must not raise."""
        empty = CommandRegistry()  # no auto-discover
        assert build_cli_flag_specs(empty) == []

    def test_command_without_cli_flag_metadata_is_skipped(self):
        """Commands that don't declare cli_flag/cli_extra_flags produce no spec."""
        from deile.commands.base import (CommandContext, CommandResult,
                                         DirectCommand)
        from deile.config.manager import CommandConfig

        class _Silent(DirectCommand):
            def __init__(self):
                super().__init__(CommandConfig(name="silent_no_flag", description="x"))

            async def execute(self, context: CommandContext) -> CommandResult:
                return CommandResult.success_result("ok")

        registry = CommandRegistry()
        registry.register_command(_Silent())
        specs = build_cli_flag_specs(registry)
        assert all(s.command_name != "silent_no_flag" for s in specs)

    def test_duplicate_cli_flag_logs_warning_and_first_wins(self, caplog):
        """Two commands declaring the same cli_flag: builder warns, keeps first."""
        import logging

        from deile.commands.base import (CommandContext, CommandResult,
                                         DirectCommand)
        from deile.config.manager import CommandConfig

        class _A(DirectCommand):
            cli_flag = "--dup-collision-test"
            cli_help = "first"

            def __init__(self):
                super().__init__(CommandConfig(name="dup_a", description="a"))

            async def execute(self, context: CommandContext) -> CommandResult:
                return CommandResult.success_result("a")

        class _B(DirectCommand):
            cli_flag = "--dup-collision-test"
            cli_help = "second"

            def __init__(self):
                super().__init__(CommandConfig(name="dup_b", description="b"))

            async def execute(self, context: CommandContext) -> CommandResult:
                return CommandResult.success_result("b")

        registry = CommandRegistry()
        registry.register_command(_A())
        registry.register_command(_B())
        with caplog.at_level(logging.WARNING, logger="deile.commands.cli_flags"):
            specs = build_cli_flag_specs(registry)
        flags = [s for s in specs if s.flag == "--dup-collision-test"]
        assert len(flags) == 1, "duplicate must be deduped (first wins)"
        assert any("Duplicate CLI flag" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 8. /version slash command exists (issue #126 marks it as missing)
# ---------------------------------------------------------------------------


class TestVersionCommand:
    def test_version_slash_command_registered(self):
        registry = _make_registry()
        assert registry.has_command("version")

    async def test_version_command_returns_version_string(self):
        from deile.commands.base import CommandContext
        from deile.commands.builtin.version_command import VersionCommand
        cmd = VersionCommand()
        ctx = CommandContext(user_input="/version", args="")
        result = await cmd.execute(ctx)
        assert result.success
        from deile.__version__ import __version__
        assert result.metadata.get("version") == __version__

    def test_version_flag_prints_version(self, no_api_keys):
        code, stdout, _ = _run_cli(["--version"])
        from deile.__version__ import __version__
        assert code == 0
        assert __version__ in stdout


# ---------------------------------------------------------------------------
# 9. --debug is a modifier, not a dispatcher
# ---------------------------------------------------------------------------


class TestDebugFlagIsModifier:
    def test_debug_alone_does_not_dispatch_one_shot_command(self, no_api_keys):
        """`deile --debug` (no message) should NOT run /debug as a one-shot.

        It must instead enter interactive mode. We patch _DeileCLI to avoid
        actually starting the REPL.
        """
        with patch("deile.cli._DeileCLI") as mock_cli_cls, \
                patch("sys.stdin.isatty", return_value=True):
            mock_instance = mock_cli_cls.return_value

            async def _fake_interactive():
                return None

            mock_instance.run_interactive.return_value = _fake_interactive()
            code, _, _ = _run_cli(["--debug"])
            mock_cli_cls.assert_called_once()
            assert code == 0

    def test_debug_flag_sets_settings_debug_enabled(self, no_api_keys):
        # Reset settings singleton so we observe the change
        import deile.config.settings as _sm
        _sm._settings = None
        with patch("deile.cli._DeileCLI") as mock_cli_cls, \
                patch("sys.stdin.isatty", return_value=True):
            mock_instance = mock_cli_cls.return_value

            async def _fake_interactive():
                return None

            mock_instance.run_interactive.return_value = _fake_interactive()
            _run_cli(["--debug"])
        from deile.config.settings import get_settings
        assert get_settings().debug_enabled is True
