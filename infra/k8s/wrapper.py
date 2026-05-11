#!/usr/bin/env python3
# ruff: noqa: E402
"""Container entrypoint wrapper — never let secrets touch /proc/<pid>/environ.

K8s passes secrets to a Pod two ways: env vars (envFrom secretRef) or
files (volumeMounts of a Secret). With env vars, the kernel records
the secret in /proc/<pid>/environ at exec time, where it stays
*forever* — even if Python pops the key from os.environ, the kernel
copy persists. Any compromised subprocess can `cat /proc/self/environ`
and exfiltrate the secret.

This wrapper inverts the flow:
  1. The Pod spec sets only non-secret env vars (HOME, DEILE_BOT_ENDPOINT,
     DEILE_BOT_APPROVAL_AUTO, etc).
  2. Secrets are mounted as files under /run/secrets/<role>/<KEY>.
  3. We read those files, inject the values into in-process os.environ
     so frameworks see them on first read, then patch
     bootstrap_providers() to pop the LLM keys back out once each
     provider has captured its copy.

Security model per role:
  deile role: LLM keys + DEILE_BOT_AUTH_TOKEN are popped from os.environ
    after bootstrap_providers() fires, so any subprocess (bash_tool,
    printenv) inherits a clean environment. /proc/<pid>/environ — frozen
    at exec time — never contained the secrets to begin with.
  bot role: LLM keys are popped via the same bootstrap hook. The Discord
    token (DEILE_BOT_DISCORD_TOKEN) and control-plane auth token
    (DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN) cannot be popped because
    discord.py holds a reference to the token value at runtime and
    bot.run() blocks for the lifetime of the process. The compensating
    control is the tool whitelist, which prevents bash_tool,
    python_execute, and file tools from being reachable from any
    Discord-driven prompt — no subprocess can be spawned to read env.

Subcommands:
  python wrapper.py deile <args ...>   → loads /run/secrets/deile/* then runs deile.cli
  python wrapper.py bot <args ...>     → loads /run/secrets/bot/*   then runs deilebot.cli
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, List

# Keys that should never survive in os.environ once bootstrap_providers()
# has copied them into each provider's in-memory state. Removing them
# means any subsequent subprocess inherits a clean env block.
_SENSITIVE_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "DEILE_BOT_AUTH_TOKEN",
    "DEILE_BOT_DISCORD_TOKEN",
    "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN",
)


def _messaging_tool_whitelist() -> frozenset:
    """Tool names the messaging whitelist permits, regardless of role.

    Derived from the messaging package itself (each tool class declares
    a ``tool_name``) so a rename in deile.tools.messaging cannot
    silently downgrade the whitelist to "deny all". If the import fails
    (deilebot extras not installed in deile) we fall back to a static
    snapshot — the caller will still see "0 kept" in logs and notice.
    """
    try:
        from deile.tools import messaging as m

        names = []
        for attr in ("DiscordSendMessageTool", "DiscordSendDMTool",
                     "DiscordReactTool", "DiscordStartThreadTool",
                     "DiscordPinMessageTool", "DiscordMentionRoleTool",
                     "DiscordGetUserProfileTool"):
            cls = getattr(m, attr, None)
            if cls is not None and getattr(cls, "tool_name", None):
                names.append(cls.tool_name)
        if names:
            return frozenset(names)
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return frozenset({
        "discord_send_message", "discord_send_dm", "discord_react",
        "discord_start_thread", "discord_pin_message",
        "discord_mention_role", "discord_get_user_profile",
    })


def _load_secret_files(role_dir: Path) -> List[str]:
    """Read every regular file under role_dir into os.environ.

    Returns the list of keys injected. Hidden files (dotfiles) and the
    K8s atomic-update symlinks (``..data``, ``..2025_…``) are skipped.
    """
    if not role_dir.exists():
        return []
    loaded: List[str] = []
    for entry in role_dir.iterdir():
        if entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            # strip() handles both LF and CRLF (Windows-formatted secret files)
            value = entry.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"wrapper: cannot read {entry}: {exc}", file=sys.stderr)
            continue
        os.environ[entry.name] = value
        loaded.append(entry.name)
    return loaded


def _harden_runtime_dirs() -> None:
    """Make sure HOME exists and its standard subdirs are writeable.

    Pre-creates ``data/`` and ``logs/`` because the bot's foundation
    expects them to exist before it opens its sqlite/log files.
    """
    home = Path(os.environ.get("HOME", "/home/deile"))
    home.mkdir(parents=True, exist_ok=True)
    (home / ".deile").mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)


def _has_llm_key(loaded: List[str]) -> bool:
    return any(k.endswith("_API_KEY") for k in loaded)


def _install_tool_whitelist(role: str) -> None:
    """Patch DeileAgent.__init__ to disable every tool outside the messaging whitelist.

    The tool registry is a module-level singleton; once disable_tool() is
    called, tools stay disabled across the process lifetime. We still patch
    __init__ as defense-in-depth in case the agent is ever reconstructed.

    The patch operates on self.tool_registry (the agent's actual registry,
    which may be a custom instance in tests) rather than the global singleton,
    so whitelist enforcement is always tied to the registry the agent uses.

    ``role`` only changes the log prefix; the policy is identical for
    bot and deile-Job.
    """
    import deile.core.agent as agent_mod

    whitelist = _messaging_tool_whitelist()
    original_init: Callable = agent_mod.DeileAgent.__init__

    def _harden_after_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        registry = self.tool_registry
        try:
            tools = registry.list_all()
        except Exception:  # noqa: BLE001 — registry is best-effort
            return
        kept, dropped = [], []
        for tool in tools:
            name = tool.name
            if name in whitelist:
                kept.append(name)
            elif registry.disable_tool(name):
                dropped.append(name)
        print(
            f"wrapper({role}): tool whitelist active — {len(kept)} kept, "
            f"{len(dropped)} disabled. kept={sorted(kept)}",
            file=sys.stderr,
        )

    agent_mod.DeileAgent.__init__ = _harden_after_init


def _patch_deile_bootstrap() -> None:
    """Make bootstrap_providers() pop *_SENSITIVE_KEYS* after providers are wired.

    Before popping we eagerly construct the bot integration settings
    (lru_cache'd) so DEILE_BOT_AUTH_TOKEN is captured in memory — the
    messaging tools that register lazily later would otherwise see an
    empty token and refuse to wire up.
    """
    import deile.core.models.bootstrap as bootstrap_mod

    original = bootstrap_mod.bootstrap_providers

    def _patched_bootstrap(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            try:
                from deile.integrations.bot import get_bot_client
                from deile.integrations.bot.config import get_bot_settings

                get_bot_settings()
                get_bot_client()
            except Exception as exc:  # noqa: BLE001 — best-effort hardening
                print(
                    "wrapper: could not pre-warm bot integration "
                    f"({type(exc).__name__}: {exc}); messaging tools may fail",
                    file=sys.stderr,
                )
            for key in _SENSITIVE_KEYS:
                os.environ.pop(key, None)

    bootstrap_mod.bootstrap_providers = _patched_bootstrap


def _run_deile(passthrough: List[str]) -> int:
    _harden_runtime_dirs()
    loaded = _load_secret_files(Path("/run/secrets/deile"))
    if not _has_llm_key(loaded):
        print(
            "wrapper(deile): no *_API_KEY found under /run/secrets/deile — "
            "deile cannot bootstrap any LLM provider.",
            file=sys.stderr,
        )
        return 78  # EX_CONFIG

    _patch_deile_bootstrap()

    # Tool policy is operator-chosen because the deile prompt comes
    # from the operator via kubectl, NOT from any external user.
    # Default = "all" so the operator can use deile for real dev
    # work inside the container; set "messaging" for unattended
    # one-shot variants whose prompt might be parameterized externally.
    policy = os.environ.get("DEILE_WRAPPER_TOOL_WHITELIST", "all").strip().lower()
    if policy == "messaging":
        try:
            _install_tool_whitelist("deile")
        except Exception as exc:  # noqa: BLE001 — refuse to start unsafe
            print(f"wrapper(deile): could not install tool whitelist: {exc}", file=sys.stderr)
            return 78
    elif policy != "all":
        print(
            f"wrapper(deile): unknown DEILE_WRAPPER_TOOL_WHITELIST={policy!r}; "
            "expected 'all' or 'messaging'",
            file=sys.stderr,
        )
        return 64

    sys.argv = ["deile", *passthrough]
    from deile.cli import main as deile_main
    return deile_main() or 0


def _run_bot(passthrough: List[str]) -> int:
    _harden_runtime_dirs()
    loaded = _load_secret_files(Path("/run/secrets/bot"))
    required = {"DEILE_BOT_DISCORD_TOKEN", "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN"}
    missing = required - set(loaded)
    if missing:
        print(
            "wrapper(bot): missing required secret files under "
            f"/run/secrets/bot: {sorted(missing)}",
            file=sys.stderr,
        )
        return 78

    # Pop LLM keys after deile's bootstrap_providers captures them.
    # DEILE_BOT_DISCORD_TOKEN and DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN cannot
    # be popped here because discord.py / the control-plane listener need them
    # for the lifetime of the process. The tool whitelist (applied below) is
    # the compensating control: without bash_tool/python_execute, no Discord
    # prompt can spawn a subprocess to read the remaining env vars.
    _patch_deile_bootstrap()

    # Discord input is untrusted — apply the whitelist whenever the
    # bot has an LLM key (i.e. its embedded agent will run prompts).
    if _has_llm_key(loaded):
        try:
            _install_tool_whitelist("bot")
        except Exception as exc:  # noqa: BLE001 — refuse to start unsafe
            print(f"wrapper(bot): could not install tool whitelist: {exc}", file=sys.stderr)
            return 78
    else:
        print(
            "wrapper(bot): no LLM key loaded — embedded agent will not run; "
            "tool whitelist not installed (no agent to constrain).",
            file=sys.stderr,
        )

    sys.argv = ["deilebot", *passthrough]
    from deilebot.cli import main as bot_main
    return bot_main() or 0


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: wrapper.py {deile|bot} <args ...>", file=sys.stderr)
        return 64  # EX_USAGE
    role, rest = argv[1], argv[2:]
    if role == "deile":
        return _run_deile(rest)
    if role == "bot":
        return _run_bot(rest)
    print(f"wrapper: unknown role {role!r} (expected 'deile' or 'bot')", file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
