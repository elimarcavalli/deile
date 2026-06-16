"""Env Command -- manage environment variables exported to the process.

Replaces the .env file approach: variables are stored in
~/.deile/settings.json under env.exports (0o600, owner-only) and loaded
into os.environ at startup, so no plaintext .env file lives in the project.

Subcommands:
    /env set KEY=VALUE      store KEY in ~/.deile/settings.json env.exports
    /env set KEY VALUE      same with space separator
    /env list               list stored vars (sensitive values masked)
    /env unset KEY          remove KEY from the store and os.environ
"""

from __future__ import annotations

from rich.table import Table

from ..base import CommandContext, CommandResult, DirectCommand


class EnvCommand(DirectCommand):
    """Manage environment variables exported to the process (replaces .env files)."""

    cli_flag = "--env"
    cli_takes_arg = True
    cli_arg_metavar = "ACTION [KEY[=VALUE]]"
    cli_help = (
        "Manage exported env vars in ~/.deile/settings.json "
        "(e.g. --env 'set ANTHROPIC_API_KEY=sk-ant-...')."
    )
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig

        super().__init__(
            CommandConfig(
                name="env",
                description=(
                    "Manage environment variables stored securely in ~/.deile/settings.json "
                    "(replaces .env files)."
                ),
            )
        )

    async def execute(self, context: CommandContext) -> CommandResult:
        args = (getattr(context, "args", "") or "").strip()
        if not args:
            return CommandResult.success_result(self.get_help(), "text")

        parts = args.split(maxsplit=1)
        subcommand = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if subcommand == "list":
            return await self._cmd_list()
        if subcommand == "set":
            return await self._cmd_set(rest)
        if subcommand == "unset":
            return await self._cmd_unset(rest)
        return CommandResult.error_result(
            "Unknown subcommand: {!r}. Use set, list, or unset.".format(subcommand)
        )

    async def _cmd_list(self) -> CommandResult:
        from ...config.env_store import list_vars

        stored = list_vars()
        if not stored:
            return CommandResult.success_result(
                "No environment variables stored.\n"
                "Use `/env set KEY=VALUE` to add one.",
                "text",
            )
        table = Table(
            title="Stored env vars (~/.deile/settings.json)",
            show_lines=False,
        )
        table.add_column("Variable", style="cyan")
        table.add_column("Value", style="dim")
        for key in sorted(stored):
            table.add_row(key, stored[key])
        return CommandResult.success_result(table, "rich")

    async def _cmd_set(self, rest: str) -> CommandResult:
        from ...config.env_store import is_sensitive, store_var

        if "=" in rest:
            key, _, value = rest.partition("=")
        else:
            parts = rest.split(maxsplit=1)
            if len(parts) < 2:
                return CommandResult.error_result(
                    "Usage: /env set KEY=VALUE  or  /env set KEY VALUE"
                )
            key, value = parts[0], parts[1]

        key = key.strip()
        value = value.strip()

        if not key:
            return CommandResult.error_result("Key must not be empty.")

        try:
            ok = store_var(key, value)
        except (ValueError, TypeError) as exc:
            return CommandResult.error_result("Cannot store variable: {}".format(exc))
        except Exception as exc:
            return CommandResult.error_result(
                "Failed to store variable: {}".format(exc)
            )

        if ok:
            display = "<masked>" if is_sensitive(key) else repr(value)
            return CommandResult.success_result(
                "Stored {}={} in ~/.deile/settings.json".format(key, display),
                "text",
            )
        return CommandResult.error_result("Failed to write settings file.")

    async def _cmd_unset(self, rest: str) -> CommandResult:
        from ...config.env_store import unset_var

        key = rest.strip()
        if not key:
            return CommandResult.error_result("Usage: /env unset KEY")

        try:
            existed = unset_var(key)
        except ValueError as exc:
            return CommandResult.error_result("Cannot unset variable: {}".format(exc))
        except Exception as exc:
            return CommandResult.error_result(
                "Failed to unset variable: {}".format(exc)
            )

        if existed:
            return CommandResult.success_result(
                "Removed {} from env store.".format(key), "text"
            )
        return CommandResult.success_result(
            "{} was not in the env store.".format(key), "text"
        )

    def get_help(self) -> str:
        return """Manage environment variables stored in ~/.deile/settings.json

Variables are loaded into os.environ at startup, replacing .env file dependency.
File is stored with 0o600 (owner-only) permissions -- no risk of accidental commits.

Usage:
  /env set KEY=VALUE      Store KEY=VALUE securely
  /env set KEY VALUE      Same with space separator
  /env list               List stored vars (sensitive values masked)
  /env unset KEY          Remove KEY from store and os.environ

Examples:
  /env set ANTHROPIC_API_KEY=sk-ant-...
  /env set MY_CUSTOM_VAR=hello
  /env list
  /env unset OLD_VAR

Variables are stored under the 'env.exports' key in ~/.deile/settings.json."""
