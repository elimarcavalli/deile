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
    "GITHUB_TOKEN",
    # GitLab tokens (issue #297) — stripped after wrapper bootstrap so they
    # never appear in /proc/self/environ. ``GITLAB_TOKEN`` is the canonical
    # name accepted by ``glab``; ``GL_TOKEN`` is the documented alias.
    "GITLAB_TOKEN",
    "GL_TOKEN",
    "DEILE_BOT_AUTH_TOKEN",
    "DEILE_BOT_DISCORD_TOKEN",
    "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN",
    # NOTE: DEILE_WORKER_BEARER_TOKEN is NOT popped — the dispatch tool
    # reads it on every call (not at bootstrap). Popping it would force
    # the tool to fall back to file reads on the hot path.
)


def _wire_worker_bearer() -> None:
    """Bot Pod: read worker-bearer Secret file and expose as env var.

    The dispatch_deile_task tool reads DEILE_WORKER_BEARER_TOKEN from the
    environment (with fallback to the file). We populate the env var here
    so the tool has it available even before the lazy file fallback.
    """
    candidates = [
        Path("/run/secrets/bot/worker/AUTH_TOKEN"),
        Path("/run/secrets/worker/AUTH_TOKEN"),
    ]
    for p in candidates:
        if p.is_file():
            try:
                tok = p.read_text(encoding="utf-8").strip()
                if tok:
                    os.environ["DEILE_WORKER_BEARER_TOKEN"] = tok
                    print(
                        f"wrapper: worker bearer wired from {p}",
                        file=sys.stderr,
                    )
                    return
            except OSError as exc:
                print(f"wrapper: cannot read {p}: {exc}", file=sys.stderr)


def _messaging_tool_whitelist() -> frozenset:
    """Tool names the bot agent is allowed to call.

    Composed of:
      - every messaging.discord_* tool that the messaging package
        declares (so renames can't silently shrink the set);
      - `dispatch_deile_task` — bot's only escape valve for real dev work;
      - `vision_describe_image` — bot needs to read images attached to DMs.

    If imports fail (deilebot_client not installed in deile) we fall
    back to a static snapshot so the bot still has a sane minimum.
    """
    names = set()
    try:
        from deile.tools import messaging as m

        for attr in ("DiscordSendMessageTool", "DiscordSendDMTool",
                     "DiscordEditMessageTool", "DiscordReactTool",
                     "DiscordStartThreadTool", "DiscordPinMessageTool",
                     "DiscordMentionRoleTool", "DiscordGetUserProfileTool"):
            cls = getattr(m, attr, None)
            if cls is not None and getattr(cls, "tool_name", None):
                names.add(cls.tool_name)
    except Exception:  # noqa: BLE001 — best-effort
        pass
    # Always include — bot's only path to real work + cron tools so it
    # can schedule from natural-language DM ("me lembre amanhã 9h de X").
    names.add("dispatch_deile_task")
    names.add("vision_describe_image")
    names.add("cron_create")
    names.add("cron_list")
    names.add("cron_delete")
    if names - {"dispatch_deile_task", "vision_describe_image",
                "cron_create", "cron_list", "cron_delete"}:
        return frozenset(names)
    # Fallback when messaging package failed to import.
    return frozenset({
        "discord_send_message", "discord_send_dm", "discord_edit_message",
        "discord_react", "discord_start_thread", "discord_pin_message",
        "discord_mention_role", "discord_get_user_profile",
        "dispatch_deile_task", "vision_describe_image",
        "cron_create", "cron_list", "cron_delete",
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

    Pre-creates ``data/``, ``logs/``, ``bin/``, and ``work/`` because
    the bot's foundation expects them to exist before it opens its
    sqlite/log files, and the git credential + clone guard helpers
    need ``bin/`` and ``work/`` to be present.
    """
    home = Path(os.environ.get("HOME", "/home/deile"))
    home.mkdir(parents=True, exist_ok=True)
    (home / ".deile").mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "data").mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "logs").mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "bin").mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "work").mkdir(parents=True, exist_ok=True, mode=0o755)


def _has_llm_key(loaded: List[str]) -> bool:
    return any(k.endswith("_API_KEY") for k in loaded)


def _atomic_write_secret(path: Path, content: str) -> None:
    """Create ``path`` at 0o600 atomically and write ``content``.

    ``O_CREAT | O_WRONLY | O_TRUNC`` with explicit mode means the file is
    never readable by anyone other than the owner — there is no TOCTOU
    window between create and chmod. Raises :class:`OSError` on failure so
    the caller can fall back / log without partial state.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    with fh:
        fh.write(content)


def _append_git_credential(creds_file: Path, host: str, token: str) -> None:
    """Append a ``https://oauth2:<token>@<host>`` line to ``~/.git-credentials``.

    git resolves credentials by exact host match, so two lines (one per
    forge) coexist peacefully. The file is rewritten with the union of
    existing lines + the new one so each call is idempotent (no duplicate
    lines for the same host) and never wipes the other forge's credential.
    """
    existing = ""
    if creds_file.exists():
        try:
            existing = creds_file.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    new_line = f"https://oauth2:{token}@{host}"
    kept = [
        line for line in existing.splitlines()
        if line.strip() and not line.strip().endswith(f"@{host}")
    ]
    kept.append(new_line)
    _atomic_write_secret(creds_file, "\n".join(kept) + "\n")


def _configure_git_global_identity() -> None:
    """Configure the global git identity + credential helper (idempotent).

    Required because every commit needs ``user.email``/``user.name``. The
    settings are global so they apply to every clone the agent creates.
    """
    import subprocess as _subprocess

    try:
        _subprocess.run(
            ["git", "config", "--global", "credential.helper", "store"],
            check=False,
        )
        # Regression observed on PR #293 (2026-05-23): without the global
        # identity every git commit dies on "unable to auto-detect email
        # address". Set the defaults here so both forges see them.
        _subprocess.run(
            ["git", "config", "--global", "user.email", "deile@deile.info"],
            check=False,
        )
        _subprocess.run(
            ["git", "config", "--global", "user.name", "DEILE-One"],
            check=False,
        )
    except OSError as exc:
        print(f"wrapper: could not update ~/.gitconfig: {exc}", file=sys.stderr)


def _setup_forge_credentials() -> None:
    """Wire ``GITHUB_TOKEN`` / ``GITLAB_TOKEN`` into the forge stacks.

    Symmetric across forges (issue #297):

    - **GitHub** (when ``GITHUB_TOKEN`` is present): line in
      ``~/.git-credentials`` for ``$DEILE_GITHUB_HOST`` (default
      ``github.com``) + ``~/.config/gh/hosts.yml`` for ``gh``.
    - **GitLab** (when ``GITLAB_TOKEN`` / ``GL_TOKEN`` is present): line in
      ``~/.git-credentials`` for ``$DEILE_GITLAB_HOST`` (default
      ``gitlab.com``) + ``~/.config/glab-cli/config.yml`` for ``glab``.

    Both env vars are removed from ``os.environ`` after the bootstrap so
    subprocesses (bash_tool, python_execute) cannot read them via
    ``/proc/self/environ`` or ``printenv``. Global git identity is
    configured exactly once regardless of which forge(s) are wired.
    """
    home = Path(os.environ.get("HOME", "/home/deile"))
    creds_file = home / ".git-credentials"
    wired_any = False

    # --- GitHub ----------------------------------------------------------
    gh_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if gh_token:
        gh_host = os.environ.get("DEILE_GITHUB_HOST", "").strip() or "github.com"
        try:
            _append_git_credential(creds_file, gh_host, gh_token)
        except OSError as exc:
            print(
                f"wrapper: could not write ~/.git-credentials (github): {exc}",
                file=sys.stderr,
            )
        else:
            wired_any = True
            print(
                f"wrapper(deile): GITHUB_TOKEN wired into ~/.git-credentials "
                f"for {gh_host}",
                file=sys.stderr,
            )
            # ``gh`` config file — same atomic 0600 write as the rest.
            try:
                gh_dir = home / ".config" / "gh"
                gh_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                _atomic_write_secret(
                    gh_dir / "hosts.yml",
                    f"{gh_host}:\n"
                    f"    oauth_token: {gh_token}\n"
                    f"    git_protocol: https\n",
                )
                print(
                    f"wrapper: GITHUB_TOKEN wired into ~/.config/gh/hosts.yml "
                    f"({gh_host})",
                    file=sys.stderr,
                )
            except OSError as exc:
                print(
                    f"wrapper: could not write ~/.config/gh/hosts.yml: {exc}",
                    file=sys.stderr,
                )
        os.environ.pop("GITHUB_TOKEN", None)

    # --- GitLab (issue #297) --------------------------------------------
    gl_token = (
        os.environ.get("GITLAB_TOKEN", "").strip()
        or os.environ.get("GL_TOKEN", "").strip()
    )
    if gl_token:
        gl_host = os.environ.get("DEILE_GITLAB_HOST", "").strip() or "gitlab.com"
        try:
            _append_git_credential(creds_file, gl_host, gl_token)
        except OSError as exc:
            print(
                f"wrapper: could not write ~/.git-credentials (gitlab): {exc}",
                file=sys.stderr,
            )
        else:
            wired_any = True
            print(
                f"wrapper(deile): GITLAB_TOKEN wired into ~/.git-credentials "
                f"for {gl_host}",
                file=sys.stderr,
            )
            try:
                glab_dir = home / ".config" / "glab-cli"
                glab_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                # ``glab`` reads ``hosts.<host>.token`` from this YAML.
                _atomic_write_secret(
                    glab_dir / "config.yml",
                    "hosts:\n"
                    f"  {gl_host}:\n"
                    f"    token: {gl_token}\n"
                    f"    api_protocol: https\n"
                    f"    api_host: {gl_host}\n",
                )
                print(
                    f"wrapper: GITLAB_TOKEN wired into "
                    f"~/.config/glab-cli/config.yml ({gl_host})",
                    file=sys.stderr,
                )
            except OSError as exc:
                print(
                    f"wrapper: could not write ~/.config/glab-cli/config.yml: {exc}",
                    file=sys.stderr,
                )
        os.environ.pop("GITLAB_TOKEN", None)
        os.environ.pop("GL_TOKEN", None)

    if wired_any:
        _configure_git_global_identity()


# Backwards-compatibility shims (issue #297): legacy bootstrap code called
# ``_setup_git_credentials`` and ``_setup_gh_auth`` separately. Both names
# now delegate to the unified ``_setup_forge_credentials`` so callers don't
# need to migrate in lock-step. The order matters only for the cosmetic
# log lines — credentials end up wired exactly once.
def _setup_git_credentials() -> None:
    _setup_forge_credentials()


def _setup_gh_auth() -> None:
    # ``_setup_forge_credentials`` writes the gh hosts.yml inline; this
    # legacy entry point becomes a no-op so calling both is harmless.
    return


def _setup_git_clone_guard(config_path: str = "") -> None:
    """Install ~/bin/git, a guard that enforces the clonable_repos allowlist.

    Reads ``git_integration.clonable_repos`` from the mounted deilebot.yaml
    config. If the config file is absent the guard allows all repos (open
    policy). If the file exists but fails to parse, the guard uses a
    fail-closed empty allowlist (deny all). Prepends ~/bin to PATH so the
    guard shadows /usr/bin/git.

    The guard is a small Python script so it runs without a shell, avoiding
    quoting / injection issues. It delegates every non-clone sub-command
    directly to /usr/bin/git unchanged.

    The allowlist patterns are baked directly into the guard script as a
    Python literal — no environment variable is used at runtime, so the
    list cannot be overwritten by subprocesses or the agent.
    """
    if not config_path:
        config_path = str(
            Path(os.environ.get("HOME", "/home/deile")) / "config/deilebot.yaml"
        )

    home = Path(os.environ.get("HOME", "/home/deile"))
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    cfg = Path(config_path)
    if not cfg.exists():
        # Config absent → open policy (allow all)
        allowlist: List[str] = ["*"]
    else:
        # Config present — parse it; any parse error → fail-closed (deny all)
        allowlist = []
        try:
            import yaml  # PyYAML is a deile dep — always available in the image

            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            raw = (data.get("git_integration") or {}).get("clonable_repos", [])
            if isinstance(raw, list) and raw:
                allowlist = [str(p).strip() for p in raw if str(p).strip()]
            else:
                # Key absent or empty list in a present config → open policy
                allowlist = ["*"]
        except Exception as exc:  # noqa: BLE001
            print(
                f"wrapper: could not parse clonable_repos from {config_path}: {exc} "
                "— fail-closed: no repos allowed",
                file=sys.stderr,
            )
            # allowlist stays [] — deny all

    # Bake the allowlist as a Python literal inside the guard script so it
    # cannot be overwritten via os.environ by subprocesses or the agent.
    # repr() of a list of strings produces a safe Python literal.
    allowlist_literal = repr(allowlist)

    guard_script = bin_dir / "git"
    guard_script.write_text(
        f"""\
#!/usr/bin/env python3
\"\"\"git clone allowlist guard — installed by wrapper.py.\"\"\"
import fnmatch, os, posixpath, subprocess, sys, urllib.parse

# Allowlist baked in at wrapper startup — not read from env at runtime.
_PATTERNS = {allowlist_literal}

args = sys.argv[1:]
# Locate the git subcommand: skip global options, handling two-token forms
# like -c KEY=VAL and -C /dir so `git -C /dir clone <url>` is not bypassed.
_OPTS_TAKES_ARG = frozenset(["-c", "-C", "--git-dir", "--work-tree",
                              "--namespace", "--super-prefix", "--exec-path"])
_subcommand = None
_sub_idx = 0
_i = 0
while _i < len(args):
    _a = args[_i]
    if _a in _OPTS_TAKES_ARG:
        _i += 2
    elif _a.startswith("-"):
        _i += 1
    else:
        _subcommand = _a
        _sub_idx = _i
        break

_git_env = None  # overridden for clone to neutralize insteadOf entries

if _subcommand == "clone":
    if _PATTERNS != ["*"]:
        urls = [a for a in args[_sub_idx + 1:] if "://" in a or a.startswith("git@")]
        if not urls:
            # No recognizable URL in clone args — deny when allowlist is active.
            print("git-clone-guard: no URL recognized in clone arguments — deny all",
                  file=sys.stderr)
            sys.exit(1)
        url = urls[0].rstrip("/")
        if url.startswith("git@"):
            # SCP-style: git@host:owner/repo.git — urlparse returns hostname=None.
            _host_path = url[len("git@"):]
            _host, _, _path = _host_path.partition(":")
            if _host.lower() == "github.com":
                repo_path = posixpath.normpath(_path.removesuffix(".git"))
            else:
                repo_path = url.removesuffix(".git")
        else:
            # Use urlparse so userinfo tricks like 'https://evil@github.com/...'
            # are rejected correctly.
            parsed = urllib.parse.urlparse(url)
            if parsed.hostname == "github.com":
                repo_path = posixpath.normpath(
                    parsed.path.lstrip("/").removesuffix(".git")
                )
            else:
                repo_path = url.removesuffix(".git")
        if not any(fnmatch.fnmatch(repo_path, p) for p in _PATTERNS):
            print(
                f"git-clone-guard: {{url!r}} is not in clonable_repos allowlist. "
                f"Allowed patterns: {{_PATTERNS}}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Strip ALL -c KEY=VALUE options for clone (deny-by-default): prevents
    # credential-helper hijacking, hook injection, proxy redirection, etc.
    # The guard re-injects only the known-safe credential.helper=store.
    _clean = []
    _ci = 0
    while _ci < len(args):
        if args[_ci] == "-c" and _ci + 1 < len(args):
            _ci += 2
        else:
            _clean.append(args[_ci])
            _ci += 1
    # Run clone in a sanitised config environment; re-inject credential.helper
    # so ~/.git-credentials written by wrapper.py is still honoured.
    _git_env = {{**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1"}}
    args = ["-c", "credential.helper=store"] + _clean

try:
    sys.exit(subprocess.run(["/usr/bin/git", *args], env=_git_env).returncode)
except FileNotFoundError:
    print("git-clone-guard: /usr/bin/git not found", file=sys.stderr)
    sys.exit(127)
""",
        encoding="utf-8",
    )
    guard_script.chmod(0o700)

    # Prepend ~/bin so the guard is found before /usr/bin/git.
    current_path = os.environ.get("PATH", "/usr/bin:/bin")
    if str(bin_dir) not in current_path.split(":"):
        os.environ["PATH"] = f"{bin_dir}:{current_path}"

    print(
        f"wrapper(deile): git clone guard active — allowlist={allowlist}",
        file=sys.stderr,
    )


def _install_tool_whitelist(role: str) -> None:
    """Disable every tool outside the messaging whitelist after auto-discover.

    Bug history: the previous implementation patched ``DeileAgent.__init__``,
    which runs BEFORE ``initialize()`` calls ``tool_registry.auto_discover()``.
    Because messaging tools are conditionally registered inside auto_discover
    (they require ``DEILE_BOT_ENDPOINT`` + token), the whitelist patch ran
    before they existed → "0 kept, 17 disabled" in production logs.

    Fix: patch ``DeileAgent.initialize`` (async), which is the function that
    actually populates the registry. After awaiting the original initialize,
    we walk the now-populated registry and disable everything outside the
    whitelist.

    The whitelist enforcement is tied to ``self.tool_registry`` (the agent's
    own registry, which may be a custom instance in tests) rather than the
    global singleton, so the policy is consistent regardless of constructor.
    """
    import asyncio as _asyncio

    import deile.core.agent as agent_mod

    whitelist = _messaging_tool_whitelist()
    original_init: Callable = agent_mod.DeileAgent.initialize

    async def _harden_after_initialize(self, *args, **kwargs):
        result = await original_init(self, *args, **kwargs)
        registry = self.tool_registry
        try:
            tools = registry.list_all()
        except Exception:  # noqa: BLE001 — registry is best-effort
            return result
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
        return result

    # Preserve the coroutine signature so callers' ``await agent.initialize()``
    # still works without warnings.
    if _asyncio.iscoroutinefunction(original_init):
        agent_mod.DeileAgent.initialize = _harden_after_initialize
    else:  # pragma: no cover — defensive: if upstream changes the signature
        def _sync_harden(self, *args, **kwargs):
            result = original_init(self, *args, **kwargs)
            registry = self.tool_registry
            try:
                tools = registry.list_all()
            except Exception:
                return result
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
            return result
        agent_mod.DeileAgent.initialize = _sync_harden


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

    # Issue #297: unified forge auth (writes gh + glab configs and the
    # ~/.git-credentials lines for whichever token(s) are present).
    _setup_forge_credentials()
    _setup_git_clone_guard()
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
    _wire_worker_bearer()
    required = {"DEILE_BOT_DISCORD_TOKEN", "DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN"}
    missing = required - set(loaded)
    if missing:
        print(
            "wrapper(bot): missing required secret files under "
            f"/run/secrets/bot: {sorted(missing)}",
            file=sys.stderr,
        )
        return 78
    # The bot's embedded agent calls dispatch_deile_task to delegate work
    # to the worker; the bot must be able to mint DEILE_BOT_AUTH_TOKEN
    # for that integration. The control-plane token is the same value; mirror.
    if "DEILE_BOT_AUTH_TOKEN" not in os.environ:
        ctok = os.environ.get("DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN", "")
        if ctok:
            os.environ["DEILE_BOT_AUTH_TOKEN"] = ctok

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


def _run_worker(passthrough: List[str]) -> int:
    """deile-worker mode: full toolset DEILE behind an HTTP API on :8766.

    Differences from `_run_deile`:
      - Loads the worker bearer Secret (not the bot Secret).
      - Skips the messaging-only whitelist (worker has full toolset by design,
        because its prompt comes from a sanitised envelope, not raw user input).
      - Bootstraps providers up-front so the first dispatch is fast.
      - Delegates to `worker_server.main()`.
    """
    _harden_runtime_dirs()
    loaded = _load_secret_files(Path("/run/secrets/deile"))
    if not _has_llm_key(loaded):
        print(
            "wrapper(worker): no *_API_KEY found under /run/secrets/deile — "
            "worker cannot bootstrap any LLM provider.",
            file=sys.stderr,
        )
        return 78
    # Make the worker bearer file readable from the standard mount point too.
    bearer = Path("/run/secrets/worker/AUTH_TOKEN")
    if bearer.is_file():
        try:
            os.environ["DEILE_WORKER_AUTH_TOKEN"] = bearer.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"wrapper(worker): cannot read worker bearer: {exc}", file=sys.stderr)
            return 78
    else:
        print(
            "wrapper(worker): worker bearer not mounted at /run/secrets/worker/AUTH_TOKEN",
            file=sys.stderr,
        )
        return 78

    # Issue #297: unified forge auth (writes gh + glab configs and the
    # ~/.git-credentials lines for whichever token(s) are present).
    _setup_forge_credentials()
    _setup_git_clone_guard()
    _patch_deile_bootstrap()

    # NEGATIVE whitelist: keep full toolset (bash/file/python/git) but
    # disable the discord messaging tools and dispatch_deile_task in the
    # worker's embedded agent. Reasoning:
    #   - The worker is who actually executes; it doesn't need to relay
    #     messages itself (the worker_server.py uses the BotClientFacade
    #     directly to post/edit status messages).
    #   - A malicious brief like "send DM to user X with phishing link"
    #     could otherwise be obeyed because DEILE_BOT_APPROVAL_AUTO=1.
    #   - dispatch_deile_task in the worker would create infinite recursion
    #     and deadlock the global _TASK_LOCK.
    try:
        _install_worker_negative_whitelist()
    except Exception as exc:  # noqa: BLE001 — refuse to start unsafe
        print(f"wrapper(worker): negative whitelist install failed: {exc}", file=sys.stderr)
        return 78

    # Delegate to the aiohttp server. It bootstraps the agent lazily
    # on first dispatch (saves cold start when the worker idles).
    sys.path.insert(0, str(Path("/app")))
    import worker_server
    return worker_server.main()


def _install_worker_negative_whitelist() -> None:
    """Disable messaging.* and dispatch_deile_task in the worker's agent.

    Worker keeps EVERYTHING ELSE (bash, file, git, pip, run_tests, etc.)
    — only what could be abused by a brief is removed.
    """
    import asyncio as _asyncio

    import deile.core.agent as agent_mod

    DROP = {
        "discord_send_message", "discord_send_dm", "discord_edit_message",
        "discord_react", "discord_start_thread", "discord_pin_message",
        "discord_mention_role", "discord_get_user_profile",
        "whatsapp_send_template", "dispatch_deile_task",
    }
    original_init = agent_mod.DeileAgent.initialize

    async def _harden(self, *args, **kwargs):
        result = await original_init(self, *args, **kwargs)
        registry = self.tool_registry
        try:
            tools = registry.list_all()
        except Exception:
            return result
        kept, dropped = [], []
        for tool in tools:
            if tool.name in DROP:
                if registry.disable_tool(tool.name):
                    dropped.append(tool.name)
            else:
                kept.append(tool.name)
        print(
            f"wrapper(worker): negative whitelist active — "
            f"{len(kept)} kept, {len(dropped)} disabled. dropped={sorted(dropped)}",
            file=sys.stderr,
        )
        return result

    if _asyncio.iscoroutinefunction(original_init):
        agent_mod.DeileAgent.initialize = _harden


def _run_pipeline(passthrough: List[str]) -> int:
    """deile-pipeline mode: run the autonomous issue → PR → merge loop.

    The pipeline Pod only ORCHESTRATES: it lists/labels issues+PRs via the
    ``gh`` CLI and dispatches the heavy implement/review work to the
    deile-worker Pod over HTTP (``DEILE_PIPELINE_DISPATCH_MODE=deile_worker``).
    It therefore needs GITHUB_TOKEN (for gh) and the worker bearer (to
    dispatch), but no LLM provider of its own — the worker carries the model.

    Differences from ``_run_worker``:
      - No provider bootstrap, no embedded agent, no tool whitelist (there is
        no agent to constrain — the loop never runs an LLM in-process).
      - No clone guard (the pipeline Pod never clones; the worker does).
    """
    _harden_runtime_dirs()
    loaded = _load_secret_files(Path("/run/secrets/deile"))
    # Forge auth (issue #297): the pipeline needs at least ONE forge token to
    # drive its loop. GitHub-only operators set GITHUB_TOKEN; GitLab-only
    # operators set GITLAB_TOKEN; dual-forge operators set both. Without
    # either, list/label calls are impossible — exit 78 ("config error").
    has_gh = "GITHUB_TOKEN" in loaded or bool(os.environ.get("GITHUB_TOKEN"))
    has_gl = (
        "GITLAB_TOKEN" in loaded
        or bool(os.environ.get("GITLAB_TOKEN"))
        or "GL_TOKEN" in loaded
        or bool(os.environ.get("GL_TOKEN"))
    )
    if not (has_gh or has_gl):
        print(
            "wrapper(pipeline): no GITHUB_TOKEN or GITLAB_TOKEN under "
            "/run/secrets/deile — the pipeline cannot drive any forge.",
            file=sys.stderr,
        )
        return 78

    # Unified setup for both forges (no-op for the absent one).
    _setup_forge_credentials()

    # Worker bearer — needed to dispatch implement/review work to the worker.
    bearer = Path("/run/secrets/worker/AUTH_TOKEN")
    if bearer.is_file():
        try:
            os.environ["DEILE_WORKER_BEARER_TOKEN"] = bearer.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"wrapper(pipeline): cannot read worker bearer: {exc}", file=sys.stderr)
            return 78
    else:
        print(
            "wrapper(pipeline): worker bearer not mounted at "
            "/run/secrets/worker/AUTH_TOKEN — dispatch_mode=deile_worker will fail.",
            file=sys.stderr,
        )

    sys.path.insert(0, str(Path("/app")))
    from deile.orchestration.pipeline.runner import main as pipeline_main
    return pipeline_main()


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("usage: wrapper.py {deile|bot|worker|pipeline} <args ...>", file=sys.stderr)
        return 64  # EX_USAGE
    role, rest = argv[1], argv[2:]
    if role == "deile":
        return _run_deile(rest)
    if role == "bot":
        return _run_bot(rest)
    if role == "worker":
        return _run_worker(rest)
    if role == "pipeline":
        return _run_pipeline(rest)
    print(
        f"wrapper: unknown role {role!r} "
        "(expected 'deile' | 'bot' | 'worker' | 'pipeline')",
        file=sys.stderr,
    )
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
