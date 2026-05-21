"""Apply Command - Gate-only stub for the retired patch applier."""

from __future__ import annotations

from pathlib import Path

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..base import CommandContext, CommandResult, DirectCommand
from ._shared import (PATCHES_DIR, list_patch_files, split_args,
                      wrap_command_errors)


class ApplyCommand(DirectCommand):
    """Stub for the retired ``/patch-apply`` command.

    Historical context: this command used to apply ``.patch`` files via an
    in-house parser/applier. The parser was fundamentally broken — it
    captured only the ``+``-prefixed lines from each hunk and discarded the
    surrounding context, so the applier ended up overwriting target files
    with ONLY the added lines (a 5-line patch against a 1000-line file
    produced a 5-line file). That was data destruction, not patching.

    The destructive machinery has been removed. The command remains in the
    registry to preserve the ``/patch-apply`` alias and to surface a clear,
    actionable redirect to the correct tooling:

    * For agent-driven edits, use the ``edit_file`` tool
      (``deile/tools/file_tools.py::EditFileTool``) — find/replace patches,
      applied atomically, validated against current file contents.
    * For operator-driven application of UNIX unified diff files, run
      ``patch -p1 < <file>`` from the shell.

    Invoking the command with no arguments still lists patches discovered
    in ``PATCHES/`` and the current directory, as a convenience for users
    who want to know what files exist before reaching for ``patch(1)``.
    """

    def __init__(self):
        from ...config.manager import CommandConfig
        config = CommandConfig(
            name="apply",
            description=(
                "[RETIRED] Legacy /patch-apply. The in-house applier was "
                "data-destructive and has been removed. Use the edit_file "
                "tool (agent) or `patch -p1 < file` (operator) instead."
            ),
            aliases=["patch-apply"],  # alias canônico mantido por retrocompatibilidade
        )
        super().__init__(config)

    @wrap_command_errors("apply")
    async def execute(self, context: CommandContext) -> CommandResult:
        """Either list available patches or surface the retirement notice."""
        parts = split_args(context)

        if not parts:
            return self._show_applicable_patches()

        return self._retirement_notice(parts[0])

    @staticmethod
    def _retirement_notice(patch_file: str) -> CommandResult:
        """Panel explaining the command is retired and pointing at replacements."""
        message = (
            "/patch-apply has been retired.\n\n"
            "The legacy in-house applier was data-destructive: its parser "
            "kept only the '+'-prefixed lines from each hunk and discarded "
            "context, then overwrote target files with just those added "
            "lines. Running it against a real file destroyed data instead "
            "of applying a patch.\n\n"
            "Use one of these instead:\n"
            "  - Agent edits:   the `edit_file` tool (find/replace patches, atomic)\n"
            "  - Operator apply: `patch -p1 < <file>` from the shell\n\n"
            f"Requested patch: {patch_file}"
        )
        return CommandResult.success_result(
            Panel(
                Text(message, style="red"),
                title="/patch-apply retired",
                border_style="red",
            ),
            "rich",
        )

    @staticmethod
    def _show_applicable_patches() -> CommandResult:
        """List patch files discoverable in PATCHES/ and the current directory."""
        patch_files = list_patch_files(extra_dirs=[Path(".")])

        if not patch_files:
            return CommandResult.success_result(
                Panel(
                    Text(
                        "No patch files found.\n\n"
                        "/patch-apply is retired — even when patches exist, they "
                        "are no longer applied in-process. Use `patch -p1 < <file>` "
                        "from the shell or the `edit_file` tool from the agent.",
                        style="yellow",
                    ),
                    title="No Patches Available",
                    border_style="yellow",
                ),
                "rich",
            )

        table = Table(
            title=f"Available Patches ({len(patch_files)} files)",
            show_header=True,
            header_style="bold blue",
        )
        table.add_column("Filename", style="cyan", width=30)
        table.add_column("Location", style="yellow", width=15)
        table.add_column("Size", style="blue", width=12)

        for patch_file in patch_files:
            location = "PATCHES/" if patch_file.parent.name == PATCHES_DIR.name else "current"
            size = f"{patch_file.stat().st_size:,}B"
            table.add_row(patch_file.name, location, size)

        usage_panel = Panel(
            Text(
                "/patch-apply is retired and no longer applies these files.\n"
                "Apply them with one of:\n"
                "  - `patch -p1 < <file>` from the shell (operator)\n"
                "  - the `edit_file` tool (agent)\n",
                style="dim",
            ),
            title="How to apply",
            border_style="dim",
        )

        return CommandResult.success_result(f"{table}\n\n{usage_panel}", "rich")

    def get_help(self) -> str:
        """Get command help."""
        return """Apply patch files (RETIRED)

Status:
  /patch-apply has been retired. The legacy in-process applier was
  data-destructive (parser kept only '+' lines, then overwrote target
  files with just those added lines). The destructive machinery has
  been removed.

Usage:
  /patch-apply                 List patches discoverable in PATCHES/ and cwd
  /patch-apply <patch_file>    Show the retirement notice

Replacements:
  - Agent edits:   the `edit_file` tool (find/replace patches, atomic)
  - Operator apply: `patch -p1 < <file>` from the shell

Related Commands:
  - /patch-generate <plan_id>  Generate a patch from a plan
  - /diff <plan_id>            Show plan changes without applying"""
