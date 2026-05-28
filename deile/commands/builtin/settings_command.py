"""Settings command — read/write ~/.deile/settings.json via slash commands.

Subcommands:
    /settings set <key> <value> [--scope=user|project]
    /settings get <key>
    /settings list [<prefix>]          aliases: ls
    /settings unset <key> [--scope=user|project]  aliases: rm
    /settings where <key>

Scope aliases: ``user`` maps to ``global`` (SettingsManager), ``project``
maps to ``project``. Default scope for writes is ``user`` (global).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, Optional

from rich.table import Table

from ..base import CommandContext, CommandResult, DirectCommand
from ..settings_manager import SettingsManager

_SCOPE_MAP = {"user": "global", "global": "global", "project": "project"}


def _parse_scope_flag(rest: str) -> tuple[str, str]:
    """Extract ``--scope=<s>`` from *rest*. Returns (cleaned_rest, scope)."""
    scope = "global"
    parts = rest.split()
    kept = []
    for tok in parts:
        if tok.startswith("--scope="):
            raw = tok[len("--scope="):].strip().lower()
            scope = _SCOPE_MAP.get(raw, "global")
        else:
            kept.append(tok)
    return " ".join(kept), scope


def _truncate(value: Any, max_len: int = 80) -> str:
    s = repr(value) if not isinstance(value, str) else value
    return s[:max_len] + "…" if len(s) > max_len else s


def _all_known_keys() -> list[str]:
    """Return sorted list of all known settings dot-paths."""
    from deile.config.settings import _JSON_ONLY_FIELD_MAP, _OVERRIDE_HANDLERS
    keys = set(_OVERRIDE_HANDLERS.keys()) | set(_JSON_ONLY_FIELD_MAP.keys())
    return sorted(keys)


def _default_value(key_path: str) -> Any:
    """Return the dataclass default for *key_path*, or None if unknown."""
    from deile.config.settings import _JSON_ONLY_FIELD_MAP, _OVERRIDE_HANDLERS, Settings

    field_name = None
    if key_path in _OVERRIDE_HANDLERS:
        field_name = _OVERRIDE_HANDLERS[key_path][0]
    elif key_path in _JSON_ONLY_FIELD_MAP:
        field_name = _JSON_ONLY_FIELD_MAP[key_path]

    if field_name is None:
        return None

    for f in dataclasses.fields(Settings):
        if f.name == field_name:
            if f.default is not dataclasses.MISSING:
                return f.default
            if f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                return f.default_factory()  # type: ignore[misc]
            return None
    return None


def _resolve_dotted_in_dict(data: dict, key_path: str) -> tuple[bool, Any]:
    node: Any = data
    for part in key_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _env_var_for_key(key_path: str) -> Optional[str]:
    """Return the env var name that overrides *key_path*, if any."""
    from deile.config.settings import _ENV_OVERRIDES, _JSON_ONLY_FIELD_MAP, _OVERRIDE_HANDLERS

    field_name = None
    if key_path in _OVERRIDE_HANDLERS:
        field_name = _OVERRIDE_HANDLERS[key_path][0]
    elif key_path in _JSON_ONLY_FIELD_MAP:
        field_name = _JSON_ONLY_FIELD_MAP[key_path]

    if field_name is None:
        return None

    for env_var, attr, _conv in _ENV_OVERRIDES:
        if attr == field_name:
            return env_var
    return None


def _coerce_value(key_path: str, raw: str) -> Any:
    """Coerce *raw* string to the appropriate Python type for *key_path*.

    Looks up the converter from ``_OVERRIDE_HANDLERS`` and applies it.
    Falls back to a best-effort heuristic (bool → int → float → str).
    """
    from deile.config.settings import _OVERRIDE_HANDLERS

    if key_path in _OVERRIDE_HANDLERS:
        _field_name, converter = _OVERRIDE_HANDLERS[key_path]
        try:
            return converter(raw)
        except (ValueError, TypeError):
            pass

    lower = raw.lower().strip()
    if lower in ("true", "yes", "on"):
        return True
    if lower in ("false", "no", "off"):
        return False
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _json_layer_origin(mgr: Any, key_path: str) -> str:
    """Return the JSON layer ('project', 'user', 'default') that wins.

    This mirrors the resolution order used by :meth:`SettingsManager.get_setting`
    (deep-merge: project overrides user). Env vars are NOT considered here —
    use :func:`_resolve_effective` for the full picture including env overrides.
    """
    proj_data = mgr.get_layer("project")
    proj_found, _ = _resolve_dotted_in_dict(proj_data, key_path)
    if proj_found:
        return "project"

    user_data = mgr.get_layer("global")
    user_found, _ = _resolve_dotted_in_dict(user_data, key_path)
    if user_found:
        return "user"

    return "default"


def _resolve_effective(mgr: Any, key_path: str) -> tuple[Any, str]:
    """Return (effective_value, origin_layer) for *key_path*.

    Resolution order (matches :class:`~deile.config.settings.Settings`):
    1. Env var override (if set) → origin ``"env"``
    2. Project JSON layer        → origin ``"project"``
    3. User/global JSON layer    → origin ``"user"``
    4. Dataclass default         → origin ``"default"``
    5. Not set                   → origin ``"not set"``

    The value for ``"env"`` origin is the raw env-var string (coerced via
    the same converter used by the Settings dataclass). For JSON layers
    the value comes from :meth:`SettingsManager.get_setting`.
    """
    from deile.config.settings import _OVERRIDE_HANDLERS

    # 1. Env var override
    env_var = _env_var_for_key(key_path)
    if env_var:
        raw = os.environ.get(env_var)
        if raw is not None and raw != "":
            # Coerce through the same converter used by Settings
            if key_path in _OVERRIDE_HANDLERS:
                _field_name, converter = _OVERRIDE_HANDLERS[key_path]
                try:
                    return converter(raw), "env"
                except (ValueError, TypeError):
                    pass
            return raw, "env"

    # 2-4. JSON layers + dataclass default
    effective = mgr.get_setting(key_path)
    if effective is not None:
        return effective, _json_layer_origin(mgr, key_path)

    default = _default_value(key_path)
    if default is not None:
        return default, "default"

    return None, "not set"


def _check_project_trust(mgr: Any) -> Optional[str]:
    """Return an error string if cwd is not in the project layer allowlist."""
    from deile.config.settings import get_settings

    settings = get_settings()
    cwd = str(Path.cwd().resolve())
    dirs = [str(Path(d).resolve()) for d in (settings.trust_project_layer_dirs or [])]
    if cwd not in dirs:
        return (
            f"Project scope is not trusted for the current directory ({cwd}).\n"
            "To enable it, run:\n"
            f"  /settings set trust.project_layer_dirs [\"{cwd}\"]"
        )
    return None


class SettingsCommand(DirectCommand):
    """Manage ~/.deile/settings.json via slash commands."""

    cli_flag = "--settings"
    cli_takes_arg = True
    cli_arg_metavar = "SUBCOMMAND [ARGS]"
    cli_help = (
        "Read/write ~/.deile/settings.json "
        "(e.g. --settings 'set pipeline.poll_interval 120')."
    )
    cli_requires_provider = False

    def __init__(self):
        from ...config.manager import CommandConfig
        super().__init__(CommandConfig(
            name="settings",
            description=(
                "Read and write ~/.deile/settings.json — "
                "set, get, list, unset, where."
            ),
            aliases=["settings"],
        ))
        self.config.aliases = ["settings"]

    @property
    def name(self) -> str:
        return "settings"

    @property
    def aliases(self) -> list:
        return []

    async def execute(self, context: CommandContext) -> CommandResult:
        args = (getattr(context, "args", "") or "").strip()
        if not args:
            return CommandResult.success_result(self._help_text(), "text")

        parts = args.split(maxsplit=1)
        subcommand = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""

        if subcommand in ("ls",):
            subcommand = "list"
        elif subcommand in ("rm",):
            subcommand = "unset"

        if subcommand == "set":
            return await self._cmd_set(rest)
        if subcommand == "get":
            return await self._cmd_get(rest)
        if subcommand == "list":
            return await self._cmd_list(rest)
        if subcommand == "unset":
            return await self._cmd_unset(rest)
        if subcommand == "where":
            return await self._cmd_where(rest)
        if subcommand in ("help", "--help", "-h"):
            return CommandResult.success_result(self._help_text(), "text")

        return CommandResult.error_result(
            f"Unknown subcommand: {subcommand!r}. "
            "Use: set, get, list (ls), unset (rm), where, or --help."
        )

    # ------------------------------------------------------------------
    # Subcommands
    # ------------------------------------------------------------------

    async def _cmd_set(self, rest: str) -> CommandResult:
        rest, scope = _parse_scope_flag(rest)
        parts = rest.split(maxsplit=1)
        if len(parts) < 2:
            return CommandResult.error_result(
                "Usage: /settings set <key> <value> [--scope=user|project]\n"
                "Example: /settings set pipeline.poll_interval 120"
            )
        key, raw_value = parts[0], parts[1]

        value = _coerce_value(key, raw_value)

        mgr = SettingsManager()

        # Project scope requires trust opt-in — check before writing.
        if scope == "project":
            err = _check_project_trust(mgr)
            if err:
                return CommandResult.error_result(err)

        try:
            ok = mgr.set_setting(key, value, scope=scope)
        except ValueError as exc:
            return CommandResult.error_result(f"Cannot set {key!r}: {exc}")
        except Exception as exc:
            return CommandResult.error_result(f"Failed to write settings: {exc}")

        if ok:
            scope_label = "user (~/.deile/settings.json)" if scope == "global" else "project (.deile/settings.json)"
            return CommandResult.success_result(
                f"Set {key} = {_truncate(value)} in {scope_label}",
                "text",
            )
        return CommandResult.error_result(
            f"Could not set {key!r}. "
            "Possible reasons: key looks like a secret, permission denied, or type mismatch."
        )

    async def _cmd_get(self, rest: str) -> CommandResult:
        key = rest.strip()
        if not key:
            return CommandResult.error_result("Usage: /settings get <key>")

        mgr = SettingsManager()
        effective, origin = _resolve_effective(mgr, key)

        table = Table(show_header=False, box=None, padding=(0, 1))
        table.add_column("Field", style="cyan")
        table.add_column("Value")
        table.add_row("key", key)
        table.add_row("value", str(effective) if effective is not None else "(not set)")
        table.add_row("origin", origin)
        return CommandResult.success_result(table, "rich")

    async def _cmd_list(self, rest: str) -> CommandResult:
        prefix = rest.strip()

        mgr = SettingsManager()

        all_keys = _all_known_keys()
        if prefix:
            all_keys = [k for k in all_keys if k.startswith(prefix)]

        if not all_keys:
            msg = f"No settings found matching prefix {prefix!r}." if prefix else "No settings found."
            return CommandResult.success_result(msg, "text")

        table = Table(title="Settings", show_lines=False)
        table.add_column("Key", style="cyan")
        table.add_column("Value", style="green")
        table.add_column("Origin", style="dim")

        for key in all_keys:
            effective, origin = _resolve_effective(mgr, key)
            value_str = _truncate(effective) if effective is not None else "(not set)"
            if effective is None and origin == "not set":
                origin = "—"
            table.add_row(key, value_str, origin)

        return CommandResult.success_result(table, "rich")

    async def _cmd_unset(self, rest: str) -> CommandResult:
        rest, scope = _parse_scope_flag(rest)
        key = rest.strip()
        if not key:
            return CommandResult.error_result(
                "Usage: /settings unset <key> [--scope=user|project]"
            )

        mgr = SettingsManager()

        if scope == "project":
            err = _check_project_trust(mgr)
            if err:
                return CommandResult.error_result(err)

        try:
            ok = mgr.unset_setting(key, scope=scope)
        except ValueError as exc:
            return CommandResult.error_result(f"Cannot unset {key!r}: {exc}")
        except Exception as exc:
            return CommandResult.error_result(f"Failed to unset setting: {exc}")

        if ok:
            scope_label = "user" if scope == "global" else "project"
            return CommandResult.success_result(
                f"Removed {key!r} from {scope_label} settings. "
                "Next read will return the layer below or the default.",
                "text",
            )
        return CommandResult.error_result(
            f"Could not unset {key!r}. Possible reasons: key looks like a secret or permission denied."
        )

    async def _cmd_where(self, rest: str) -> CommandResult:
        key = rest.strip()
        if not key:
            return CommandResult.error_result("Usage: /settings where <key>")

        mgr = SettingsManager()

        default_val = _default_value(key)
        user_path = mgr.global_settings_path
        project_path = mgr.project_settings_path

        user_data = mgr.get_layer("global")
        project_data = mgr.get_layer("project")

        user_found, user_val = _resolve_dotted_in_dict(user_data, key)
        proj_found, proj_val = _resolve_dotted_in_dict(project_data, key)

        env_var = _env_var_for_key(key)
        env_val_raw = os.environ.get(env_var) if env_var else None
        # Coerce env value to match the resolved effective value
        env_val: Any = None
        env_has_value = False
        if env_val_raw is not None and env_val_raw != "":
            from deile.config.settings import _OVERRIDE_HANDLERS
            env_has_value = True
            if key in _OVERRIDE_HANDLERS:
                _field_name, converter = _OVERRIDE_HANDLERS[key]
                try:
                    env_val = converter(env_val_raw)
                except (ValueError, TypeError):
                    env_val = env_val_raw
            else:
                env_val = env_val_raw

        effective, origin = _resolve_effective(mgr, key)

        table = Table(title=f"Settings layers for: {key}", show_lines=True)
        table.add_column("Layer", style="cyan")
        table.add_column("Value", style="green")
        table.add_column("Source", style="dim")
        table.add_column("Wins?")

        def _wins(layer: str) -> str:
            return "[bold green]← wins[/bold green]" if origin == layer else ""

        table.add_row(
            "default",
            _truncate(default_val) if default_val is not None else "—",
            "(dataclass default)",
            _wins("default"),
        )
        table.add_row(
            "user",
            _truncate(user_val) if user_found else "— (not set)",
            str(user_path),
            _wins("user"),
        )
        table.add_row(
            "project",
            _truncate(proj_val) if proj_found else "— (not set)",
            str(project_path),
            _wins("project"),
        )
        if env_has_value:
            env_display = env_var if env_var else "(no env var for this key)"
            table.add_row(
                "env",
                _truncate(env_val),
                env_display,
                _wins("env"),
            )
        else:
            env_display = env_var if env_var else "(no env var for this key)"
            table.add_row(
                "env",
                "— (not set)",
                env_display,
                _wins("env"),
            )

        summary = (
            f"\nEffective value: [bold]{_truncate(effective) if effective is not None else '(not set)'}[/bold]"
            f"  (origin: {origin})"
        )
        from rich.console import Group
        from rich.text import Text
        content = Group(table, Text.from_markup(summary))
        return CommandResult.success_result(content, "rich")

    # ------------------------------------------------------------------
    # Help
    # ------------------------------------------------------------------

    def _help_text(self) -> str:
        return """\
Manage ~/.deile/settings.json — layered settings for DEILE.

Usage:
  /settings set <key> <value> [--scope=user|project]
      Write a value. Default scope: user (~/.deile/settings.json).
      Example: /settings set pipeline.poll_interval 120

  /settings get <key>
      Show the effective value and its origin layer.
      Example: /settings get pipeline.poll_interval

  /settings list [<prefix>]         (alias: ls)
      List all settings, optionally filtered by prefix.
      Example: /settings list pipeline

  /settings unset <key> [--scope=user|project]   (alias: rm)
      Remove a key from the specified scope (falls back to lower layer).
      Example: /settings unset pipeline.poll_interval

  /settings where <key>
      Show each layer's value and which one wins.
      Example: /settings where pipeline.poll_interval

Notes:
  • Keys matching secret patterns (token, key, secret, password, api_) are blocked.
  • Project scope requires opt-in via trust.project_layer_dirs in ~/.deile/settings.json.
  • Type coercion is automatic: "true"/"false" → bool, numeric strings → int/float."""
