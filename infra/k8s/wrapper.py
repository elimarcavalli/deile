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
import re
import shlex
import sys
import warnings
from pathlib import Path
from typing import Callable, List

# ConfigMap montado pelo manifest do claude-worker. O arquivo deve conter
# uma regex por linha — linhas vazias e iniciadas em ``#`` são ignoradas.
# Caminho default; override por env var ``DEILE_CLAUDE_ALLOWED_REPOS_FILE``
# para facilitar testes locais.
CLAUDE_ALLOWED_REPOS_FILE = "/etc/claude-worker/allowed_repos.regex"

# Keys that should never survive in os.environ once bootstrap_providers()
# has copied them into each provider's in-memory state. Removing them
# means any subsequent subprocess inherits a clean env block.
_SENSITIVE_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
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


def _wire_monitor_bearer() -> None:
    """Bot Pod: read monitor-bearer Secret file and expose as env var.

    The MonitorClient (deile/infrastructure/deile_monitor_client.py) reads
    DEILE_MONITOR_AUTH_TOKEN from the environment (with fallback to the
    secret file). We populate the env var here so the /monitor cog can
    authenticate to monitor_command_server (:8769) on the first call,
    before any lazy file fallback. Mirror of _wire_worker_bearer().
    """
    candidates = [
        Path("/run/secrets/bot/monitor/MONITOR_BEARER_TOKEN"),
        Path("/run/secrets/monitor/MONITOR_BEARER_TOKEN"),
    ]
    for p in candidates:
        if p.is_file():
            try:
                tok = p.read_text(encoding="utf-8").strip()
                if tok:
                    os.environ["DEILE_MONITOR_AUTH_TOKEN"] = tok
                    print(
                        f"wrapper: monitor bearer wired from {p}",
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

    Dedup é feita por **hostname canônico** (via ``urlparse``), não por
    ``endswith(f"@{host}")``: linhas que carregam path (ex.
    ``https://oauth2:TOK@github.com/owner/repo``) ou outras combinações
    válidas seriam preservadas em paralelo com uma duplicata pelo dedup
    naive baseado em sufixo.
    """
    from urllib.parse import urlparse as _urlparse

    existing = ""
    if creds_file.exists():
        try:
            existing = creds_file.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    new_line = f"https://oauth2:{token}@{host}"
    host_lc = host.lower()
    kept: List[str] = []
    for line in existing.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            line_host = (_urlparse(stripped).hostname or "").lower()
        except ValueError:
            line_host = ""
        if line_host == host_lc:
            continue
        kept.append(stripped)
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


_setup_gh_auth_warned: bool = False


def _setup_gh_auth() -> None:
    """Deprecated compat-shim for ``_setup_forge_credentials``.

    .. deprecated::
        ``_setup_gh_auth`` was replaced by ``_setup_forge_credentials``, which
        handles both GitHub and GitLab credentials uniformly (issue #297).
        This shim delegates to ``_setup_forge_credentials`` and emits a
        ``DeprecationWarning`` on the first invocation per process.  Remove
        calls to this function — use ``_setup_forge_credentials`` directly.
    """
    global _setup_gh_auth_warned
    if not _setup_gh_auth_warned:
        warnings.warn(
            "_setup_gh_auth() is deprecated and will be removed in a future release; "
            "call _setup_forge_credentials() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        _setup_gh_auth_warned = True
    # A função _setup_forge_credentials já gerencia o hosts.yml do gh; chamar
    # ambas em sequência é inofensivo (o arquivo é sobrescrito idempotentemente).
    _setup_forge_credentials()


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

    # Bake também as listas de hosts conhecidas (GitHub e GitLab) — assim o
    # guard pode normalizar SSH/HTTPS de ambos os forges para o caminho
    # canônico (``owner/repo`` ou ``group/.../project``) antes de comparar
    # com o allowlist. Sem isto o GitHub é normalizado mas o GitLab cai no
    # ``url.removesuffix(".git")``, quebrando a simetria forge-agnostic
    # (issue #297).
    _gh_extra = [
        h.strip().lower() for h in os.environ.get("DEILE_GITHUB_HOST", "")
        .replace(",", " ").split() if h.strip()
    ]
    _gl_extra = [
        h.strip().lower() for h in os.environ.get("DEILE_GITLAB_HOST", "")
        .replace(",", " ").split() if h.strip()
    ]
    gh_hosts_literal = repr(["github.com", *_gh_extra])
    gl_hosts_literal = repr(["gitlab.com", *_gl_extra])

    guard_script = bin_dir / "git"
    guard_script.write_text(
        f"""\
#!/usr/bin/env python3
\"\"\"git clone allowlist guard — installed by wrapper.py.\"\"\"
import fnmatch, os, posixpath, subprocess, sys, urllib.parse

# Allowlist baked in at wrapper startup — not read from env at runtime.
_PATTERNS = {allowlist_literal}
_GH_HOSTS = {gh_hosts_literal}
_GL_HOSTS = {gl_hosts_literal}

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
        # Normalize GitHub e GitLab (cloud + declared hosts) para o caminho
        # canônico antes de comparar com o allowlist — assim padrões como
        # ``owner/repo`` ou ``group/sub/project`` valem para ambos forges.
        if url.startswith("git@"):
            # SCP-style: git@host:path.git — urlparse retorna hostname=None.
            _host_path = url[len("git@"):]
            _host, _, _path = _host_path.partition(":")
            _host = _host.lower()
            if _host in _GH_HOSTS or _host in _GL_HOSTS:
                repo_path = posixpath.normpath(_path.removesuffix(".git"))
            else:
                repo_path = url.removesuffix(".git")
        else:
            # Use urlparse so userinfo tricks like 'https://evil@github.com/...'
            # are rejected correctly.
            parsed = urllib.parse.urlparse(url)
            _host = (parsed.hostname or "").lower()
            if _host in _GH_HOSTS or _host in _GL_HOSTS:
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


def _load_org_tool_allow_list() -> frozenset:
    """Lê o allow-list de tools da org a partir de ``Settings.org_tool_allow_list``.

    Retorna um frozenset vazio quando não há configuração de org — o comportamento
    é então idêntico ao baseline (nenhuma interseção aplicada). Qualquer falha de
    import ou leitura é absorvida e logada, nunca falha-aberta.
    """
    try:
        from deile.config.settings import get_settings
        allow = get_settings().org_tool_allow_list
        if allow:
            return frozenset(allow)
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(
            f"wrapper: falha ao ler org_tool_allow_list das Settings: {exc}",
            file=sys.stderr,
        )
    return frozenset()


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

    Monotonicidade (issue #741): se ``Settings.org_tool_allow_list`` for
    não-vazio, o whitelist efetivo é a *interseção* com o allow-list da org —
    nunca a *união*. Sem allow-list de org, comportamento idêntico ao baseline.
    """
    import asyncio as _asyncio

    import deile.core.agent as agent_mod

    role_whitelist = _messaging_tool_whitelist()
    org_allow = _load_org_tool_allow_list()
    # Monotonicidade: org só estreita, nunca amplia.
    whitelist = role_whitelist & org_allow if org_allow else role_whitelist
    if org_allow:
        narrowed = role_whitelist - whitelist
        if narrowed:
            print(
                f"wrapper({role}): org allow-list removeu {len(narrowed)} tool(s): "
                f"{sorted(narrowed)}",
                file=sys.stderr,
            )
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
    _wire_monitor_bearer()
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


def _load_allowed_repo_patterns() -> List[re.Pattern]:
    """Carrega regexes do ConfigMap ``claude-worker-allowed-repos``.

    Cada linha não-vazia e não-comentário (``#``) é compilada como regex.
    Sem allowlist, NÃO arrancamos o claude-worker — defense-in-depth
    contra prompt-injection que tentasse ``git push`` para repositório
    arbitrário. Por isso falhamos hard (``sys.exit``) em três cenários:

    1. arquivo ausente;
    2. arquivo só com comentários/linhas em branco (allowlist vazia);
    3. qualquer linha com regex sintaticamente inválida.

    O caminho do arquivo pode ser sobrescrito via
    ``DEILE_CLAUDE_ALLOWED_REPOS_FILE`` (usado em testes; default é o
    mount-point do ConfigMap em produção).
    """
    path = Path(
        os.environ.get(
            "DEILE_CLAUDE_ALLOWED_REPOS_FILE", CLAUDE_ALLOWED_REPOS_FILE,
        )
    )
    if not path.exists():
        sys.exit(f"FATAL: claude-worker allowed-repos config missing: {path}")
    patterns: List[re.Pattern] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            patterns.append(re.compile(stripped))
        except re.error as exc:
            sys.exit(
                f"FATAL: invalid regex in {path}: {stripped!r}: {exc}"
            )
    if not patterns:
        sys.exit(
            f"FATAL: empty allowed-repos config "
            f"(no non-comment lines): {path}"
        )
    return patterns


def _install_git_repo_guard(allowed: List[re.Pattern]) -> None:
    """Sinaliza para ``claude_worker_server`` que a allowlist foi carregada.

    O modelo V1 mantém a verificação efetiva no próprio
    ``claude_worker_server`` (que recarrega o mesmo arquivo apontado por
    ``DEILE_CLAUDE_ALLOWED_REPOS_FILE``). Aqui só publicamos um marcador
    para que ele possa falhar cedo se ``wrapper.py`` não tiver passado
    pela validação prévia.

    Se quisermos um pre-receive hook em ``~/.gitconfig`` (ex.: gancho que
    bloqueia ``git fetch``/``push`` de URL fora da lista), o lugar para
    instalá-lo é aqui — em follow-up dedicado.
    """
    os.environ["DEILE_CLAUDE_ALLOWED_REPOS_LOADED"] = "1"
    # ``len(allowed)`` é exposto apenas para observabilidade nos logs do
    # claude_worker_server; nenhuma decisão deve depender deste número.
    os.environ["DEILE_CLAUDE_ALLOWED_REPOS_COUNT"] = str(len(allowed))


def _run_claude_worker(passthrough: List[str]) -> int:
    """claude-worker mode: servidor HTTP que orquestra ``claude -p`` por dispatch.

    Diferenças versus ``_run_worker`` (deile-worker):
      - **Não** carrega ``*_API_KEY`` próprios — o ``claude`` CLI usa
        autenticação por assinatura do Claude Code (sem env var), e
        ``ANTHROPIC_API_KEY`` é explicitamente removido para evitar
        que o subprocess prefira a chave API a token de assinatura.
      - Carrega a allowlist regex de repositórios antes de qualquer
        outra inicialização (fail-fast).
      - Delega para ``claude_worker_server.main()`` (criado em Task 12);
        se o módulo ainda não existe, abortamos com mensagem clara.
    """
    _harden_runtime_dirs()
    # Allowlist de repositórios — fail-fast se ausente.
    patterns = _load_allowed_repo_patterns()
    _install_git_repo_guard(patterns)

    # Bearer do claude-worker. Montado pelo manifest 50 em
    # ``/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN``. O valor
    # é sincronizado com ``worker-bearer`` do deile-worker (por
    # ``_kubectl_sync_bearer_token`` no ``deploy.py k8s claude-login``)
    # para que o ``DeileWorkerClient`` no pipeline envie o mesmo Bearer
    # independente do destino. O servidor (``claude_worker_server``) lê
    # esse arquivo direto via ``_read_auth_token`` — exportamos a env
    # var apenas como fallback / diagnóstico.
    bearer = Path("/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN")
    if bearer.is_file():
        try:
            token = bearer.read_text(encoding="utf-8").strip()
            os.environ["DEILE_CLAUDE_WORKER_AUTH_TOKEN"] = token
        except OSError as exc:
            print(
                f"wrapper(claude-worker): cannot read claude-worker bearer: {exc}",
                file=sys.stderr,
            )
            return 78
    else:
        print(
            "wrapper(claude-worker): bearer not mounted at "
            "/run/secrets/claude-worker/CLAUDE_WORKER_BEARER_TOKEN — "
            "rode `deploy.py k8s claude-login` para popular o Secret",
            file=sys.stderr,
        )
        return 78

    # GITHUB_TOKEN (e outras) podem estar mounted para que o ``claude`` CLI
    # possa clonar/comitar — fluxo idêntico aos demais papéis.
    _load_secret_files(Path("/run/secrets/deile"))
    _setup_forge_credentials()

    # Auth (issue #603): o ``claude -p`` autentica via CLAUDE_CODE_OAUTH_TOKEN
    # (setup-token, ~1 ano). ANTHROPIC_API_KEY tem precedência sobre o token na
    # cadeia de auth do CLI — se vazasse pro subprocess, cobraria via API key
    # (não assinatura) e poderia quebrar a frota. Removido explicitamente do
    # env do pod ANTES de qualquer dispatch (o ``deile`` role faz o mesmo via
    # ``_patch_deile_bootstrap``; aqui não há bootstrap de providers).
    if os.environ.pop("ANTHROPIC_API_KEY", None) is not None:
        print(
            "wrapper(claude-worker): ANTHROPIC_API_KEY removido do env "
            "(auth via CLAUDE_CODE_OAUTH_TOKEN — issue #603)",
            file=sys.stderr,
        )

    # Política V1: NÃO carregamos provedores LLM nem instalamos whitelist
    # do agente DEILE in-process. O claude_worker_server gerencia a CLI
    # claude por subprocess; o sandboxing efetivo é a allowlist de repos
    # + NetworkPolicy + filesystem read-only fora do ``/home/claude/work``.

    sys.path.insert(0, str(Path("/app")))
    try:
        # type: ignore[import-not-found]
        from claude_worker_server import main as server_main
    except ImportError as exc:
        sys.exit(
            "FATAL: claude_worker_server module not found in /app/ — "
            f"Task 12 must create this script. ImportError: {exc}"
        )
    return server_main(passthrough) if passthrough else server_main()


def _run_cli_worker(passthrough: List[str]) -> int:
    """cli-worker mode: servidor genérico da frota multi-CLI (opencode/aider/...).

    Espelha :func:`_run_claude_worker` SEM o OAuth do claude (os CLI workers usam
    API key via env, montada do Secret ``cli-worker-keys`` direto no Deployment).
    Faz, antes de subir o ``cli_worker_server``:

      - ``_harden_runtime_dirs``;
      - carrega a allowlist de repositórios (fail-fast) + instala o guard de git
        (mesma defesa do claude-worker contra push para repo arbitrário);
      - lê o Bearer do Secret ``cli-worker`` e o exporta como
        ``DEILE_CLI_WORKER_AUTH_TOKEN`` (o server o lê via ``_read_auth_token``);
      - carrega ``/run/secrets/deile/*`` e wira ``GITHUB_TOKEN``/``GITLAB_TOKEN``
        + a **identidade git global** (``user.name``/``user.email`` +
        ``credential.helper=store``) via :func:`_setup_forge_credentials` — sem
        isto o ciclo de repo do server (clone/commit/push) falha em
        "unable to auto-detect email address" e o push fica sem credencial.

    O server escolhe o adapter por ``DEILE_CLI_WORKER_KIND`` e clona o repo +
    faz checkout do branch por dispatch (ver ``cli_worker_server``).
    """
    _harden_runtime_dirs()
    # Allowlist de repositórios — fail-fast se ausente (mesma defesa do claude).
    patterns = _load_allowed_repo_patterns()
    _install_git_repo_guard(patterns)

    # Bearer do cli-worker. Montado pelo manifest gerado em
    # ``/run/secrets/cli-worker/CLI_WORKER_BEARER_TOKEN``. O server lê o arquivo
    # direto via ``_read_auth_token``; exportamos a env var como fallback.
    bearer = Path("/run/secrets/cli-worker/CLI_WORKER_BEARER_TOKEN")
    if bearer.is_file():
        try:
            token = bearer.read_text(encoding="utf-8").strip()
            if token:
                os.environ["DEILE_CLI_WORKER_AUTH_TOKEN"] = token
        except OSError as exc:
            print(
                f"wrapper(cli-worker): cannot read cli-worker bearer: {exc}",
                file=sys.stderr,
            )
            return 78
    else:
        print(
            "wrapper(cli-worker): bearer not mounted at "
            "/run/secrets/cli-worker/CLI_WORKER_BEARER_TOKEN — "
            "rode `deploy.py k8s cli-worker-install <kind>` para popular o Secret",
            file=sys.stderr,
        )
        return 78

    # GITHUB_TOKEN / GITLAB_TOKEN para clone/commit/push + identidade git global.
    # As *_API_KEY do LLM vêm do Secret ``cli-worker-keys`` direto como env no
    # Deployment (o adapter as declara em ``auth_env_keys``); NÃO as carregamos
    # aqui (não passam por ``/run/secrets/deile``).
    _load_secret_files(Path("/run/secrets/deile"))
    _setup_forge_credentials()

    sys.path.insert(0, str(Path("/app")))
    try:
        from cli_worker_server import main as server_main  # type: ignore[import-not-found]
    except ImportError as exc:
        sys.exit(
            "FATAL: cli_worker_server module not found in /app/ — "
            f"ImportError: {exc}"
        )
    return server_main(passthrough) if passthrough else server_main()


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


def _run_monitor(passthrough: List[str]) -> int:
    """deile-monitor mode: autonomous cluster supervisor running with the monitor persona.

    One invocation = one tick. The Deployment wraps this call in a shell loop
    that drives the cadence; this function bootstraps DEILE with the ``monitor``
    persona and forwards ``passthrough`` (the tick prompt) to the CLI, which
    runs in one-shot mode and exits when the turn finishes. The persona
    (``deile/personas/instructions/monitor.md``) drives all behavior — no
    Python logic is embedded here. Hot-reload of the persona is handled by the
    existing ``watchdog`` mechanism.

    Why one-shot per tick (not an in-process loop): the persona is the single
    source of truth for cadence, vigias and actions; the shell loop in the
    Deployment is the heartbeat. Each tick is a fresh DEILE process — state
    persistence is via the PVC at ``/state`` (audit log, tick state JSON,
    pause flag, command queue). This keeps the model purely prompt-first.

    Security posture:
      - Loads secrets from ``/run/secrets/deile`` (same Secret as deile-worker).
      - Requires GITHUB_TOKEN for ``gh`` CLI calls (forge queries).
      - Full tool whitelist (bash/file/find) — the prompt comes from the operator
        (this file), not from any external untrusted source.
      - Messaging tools are kept so the monitor can notify via ``curl`` calls to
        the bot; ``dispatch_deile_task`` is dropped (monitor never dispatches work).
    """
    _harden_runtime_dirs()
    loaded = _load_secret_files(Path("/run/secrets/deile"))
    if not _has_llm_key(loaded):
        print(
            "wrapper(monitor): no *_API_KEY found under /run/secrets/deile — "
            "monitor cannot bootstrap any LLM provider.",
            file=sys.stderr,
        )
        return 78  # EX_CONFIG

    has_gh = "GITHUB_TOKEN" in loaded or bool(os.environ.get("GITHUB_TOKEN"))
    has_gl = (
        "GITLAB_TOKEN" in loaded
        or bool(os.environ.get("GITLAB_TOKEN"))
        or "GL_TOKEN" in loaded
        or bool(os.environ.get("GL_TOKEN"))
    )
    if not (has_gh or has_gl):
        print(
            "wrapper(monitor): no GITHUB_TOKEN or GITLAB_TOKEN under "
            "/run/secrets/deile — the monitor cannot query the forge.",
            file=sys.stderr,
        )
        return 78

    _setup_forge_credentials()
    _patch_deile_bootstrap()

    # Drop dispatch_deile_task — the monitor never dispatches coding work
    # (it only supervises). Keep bash/file/find for kubectl/gh/curl calls.
    try:
        _install_monitor_negative_whitelist()
    except Exception as exc:  # noqa: BLE001 — refuse to start unsafe
        print(f"wrapper(monitor): negative whitelist install failed: {exc}", file=sys.stderr)
        return 78

    # Run DEILE with the monitor persona. The CLI does not accept --persona;
    # persona selection is done via DEILE_DEFAULT_PERSONA env var (set above).
    # Passthrough carries the one-shot message from the shell-loop heartbeat.
    os.environ.setdefault("DEILE_DEFAULT_PERSONA", "monitor")
    sys.argv = ["deile", *passthrough]
    from deile.cli import main as deile_main
    return deile_main() or 0


def _install_monitor_negative_whitelist() -> None:
    """Drop dispatch_deile_task from the monitor agent.

    The monitor supervises the cluster; it never dispatches coding tasks.
    Keeping dispatch_deile_task would allow a compromised prompt to spawn
    workers and consume LLM budget.
    """
    import asyncio as _asyncio

    import deile.core.agent as agent_mod

    DROP = {"dispatch_deile_task"}
    original_init = agent_mod.DeileAgent.initialize

    async def _harden(self, *args, **kwargs):
        result = await original_init(self, *args, **kwargs)
        registry = self.tool_registry
        try:
            tools = registry.list_all()
        except Exception:
            return result
        dropped = []
        for tool in tools:
            if tool.name in DROP:
                if registry.disable_tool(tool.name):
                    dropped.append(tool.name)
        if dropped:
            print(
                f"wrapper(monitor): negative whitelist active — "
                f"dropped={sorted(dropped)}",
                file=sys.stderr,
            )
        return result

    if _asyncio.iscoroutinefunction(original_init):
        agent_mod.DeileAgent.initialize = _harden


# Read-only Q&A uses an ALLOW-LIST executor, not a denylist. A denylist over a
# string handed to a real shell is bypassable (command substitution, indirection,
# unlisted binaries like `curl`/`python3`). Instead the monitor-qa bash tool is
# replaced by a SHELL-FREE runner (subprocess shell=False) that only runs a fixed
# set of read binaries with read verbs — so chaining (`;`/`|`/`&&`), substitution
# (`$()`/backticks) and redirection (`>`) are never interpreted, regardless of the
# prompt. Safe-by-construction, not safe-by-instruction.
# Coreutils here are all STDOUT-only (no output-file flag). `sort`/`uniq` are
# deliberately EXCLUDED: `sort -o FILE` / `uniq IN OUT` can write a file, which
# breaks the read-only-by-construction guarantee (and they need pipes — which we
# don't have — to be useful anyway).
_QA_ALLOWED_BINARIES = frozenset({
    "kubectl", "gh", "glab", "cat", "ls", "head", "tail", "grep", "egrep",
    "wc", "jq", "cut", "nl", "tac", "tr", "column", "echo", "date",
})
# `kubectl config` has mutating subcommands; only these read it.
_QA_KUBECTL_CONFIG_READ = frozenset({
    "view", "get-contexts", "current-context", "get-clusters", "get-users",
})
_QA_KUBECTL_READ_VERBS = frozenset({
    "get", "describe", "logs", "top", "explain", "api-resources",
    "cluster-info", "events", "version", "config",
})
# gh/glab grammar is `<binary> <noun> <action>` (e.g. `gh pr merge`). Allow-list
# the READ action (2nd non-flag token) rather than denylist writes — "run"/"pr"
# are read NOUNS (`gh run list`) yet "create"/"merge" are write ACTIONS.
_QA_FORGE_READ_ACTIONS = frozenset({
    "list", "view", "status", "diff", "checks", "get", "show", "ls", "describe",
})
_QA_FORGE_READ_TOPLEVEL = frozenset({"status", "version"})
# Paths whose contents are secrets — never readable via the Q&A read binaries.
_QA_DENY_PATH_RE = re.compile(
    r"/run/secrets|/var/run/secrets|\.git-credentials|\.config/(?:gh|glab)|/proc/",
    re.IGNORECASE,
)


def _qa_api_method(argv: List[str]) -> str:
    """HTTP method of a gh/glab ``api`` call across ALL flag forms — ``-X GET``,
    ``-XGET``, ``--method GET``, ``--method=GET``. Default ``GET``. (Token-shape
    naivety here is what let ``-XDELETE`` slip past the first cut.)"""
    method = "GET"
    for i, a in enumerate(argv):
        if a in ("-X", "--method"):
            if i + 1 < len(argv):
                method = argv[i + 1]
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:]
        elif a.startswith("--method="):
            method = a.split("=", 1)[1]
    return method


def _qa_is_field_flag(token: str) -> bool:
    """True if *token* is a gh/glab ``api`` field/body flag (a WRITE), any form:
    ``-f`` / ``-fkey=v`` / ``-F`` / ``--field[=v]`` / ``--raw-field[…]`` / ``--input[…]``."""
    return token.startswith(("-f", "-F", "--field", "--raw-field", "--input"))


def _qa_command_allowed(cmd: str) -> "tuple[bool, str]":
    """Decide whether *cmd* is a permitted READ-ONLY command. Pure + testable.

    Allow-list by construction: parse argv with ``shlex`` (no shell), require the
    binary to be in :data:`_QA_ALLOWED_BINARIES`, constrain kubectl/gh/glab to
    read verbs (no Secret reads, no `--raw`, no `gh api` writes), and deny any
    path under a secret mount. Returns ``(ok, reason)``."""
    cmd = (cmd or "").strip()
    if not cmd:
        return False, "comando vazio"
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False, "comando não parseável"
    if not argv:
        return False, "comando vazio"
    binary = os.path.basename(argv[0])
    if binary not in _QA_ALLOWED_BINARIES:
        return False, f"binário '{binary}' não permitido (modo somente-leitura)"
    if _QA_DENY_PATH_RE.search(cmd):
        return False, "acesso a caminhos de segredo negado"
    if binary == "kubectl":
        # Reject raw-API + credential/endpoint-override flags in EVERY form
        # (incl. attached `--raw=PATH`, `--as=...`). `--raw=` was the bypass that
        # read Secrets past the verb check; `--as` is impersonation; the agent
        # uses the pod KUBECONFIG so `--server/--token/--kubeconfig` are vectors.
        if any(a.startswith(("--raw", "--as", "--token", "--server", "--kubeconfig"))
               for a in argv):
            return False, "kubectl: flag não permitida (--raw/--as/--token/--server/--kubeconfig)"
        verb = next((a for a in argv[1:] if not a.startswith("-")), "")
        if verb not in _QA_KUBECTL_READ_VERBS:
            return False, f"kubectl '{verb}' não é um verbo de leitura permitido"
        if verb == "config":
            sub = next((a for a in argv[2:] if not a.startswith("-")), "")
            if sub not in _QA_KUBECTL_CONFIG_READ:
                return False, f"kubectl config '{sub or '?'}' não é somente-leitura"
        if verb in ("get", "describe") and any(
            a in ("secret", "secrets") or a.startswith(("secret/", "secrets/"))
            for a in argv[1:]
        ):
            return False, "leitura de Secrets negada"
    if binary in ("gh", "glab"):
        # Token-disclosure is never allowed (gh/glab auth status --show-token,
        # glab config get token, auth token, ...).
        if any(a == "--show-token" for a in argv):
            return False, f"{binary}: --show-token negado"
        nonflag = [a for a in argv[1:] if not a.startswith("-")]
        noun = nonflag[0] if nonflag else ""
        action = nonflag[1] if len(nonflag) > 1 else ""
        if noun == "config":
            return False, f"{binary} config negado (pode expor o token)"
        if noun == "auth":
            # only `auth status` and never with a token-printing flag.
            if action != "status" or any(a == "-t" for a in argv):
                return False, f"{binary} auth '{action or '?'}' negado (somente-leitura)"
            return True, ""
        if noun == "api":
            method = _qa_api_method(argv)
            if method.upper() not in ("GET", "HEAD"):
                return False, f"{binary} api método '{method}' negado (só GET)"
            if any(_qa_is_field_flag(a) for a in argv):
                return False, f"{binary} api com campo/corpo (escrita) negado"
            if any("secrets" in a for a in nonflag[1:]):
                return False, f"{binary} api a endpoint de secrets negado"
            return True, ""
        if action:
            if action not in _QA_FORGE_READ_ACTIONS:
                return False, f"{binary} '{noun} {action}' não é uma ação de leitura permitida"
        elif noun not in _QA_FORGE_READ_TOPLEVEL:
            return False, f"{binary} '{noun}' não permitido (somente-leitura)"
    return True, ""


def _wrap_bash_readonly(bash_tool: object) -> None:
    """Replace the bash tool with a shell-FREE allow-list read executor.

    The command is parsed with ``shlex`` and executed via
    ``subprocess.run(argv, shell=False)`` — so a prompt-injected chain /
    substitution / redirection is never interpreted, and only the binaries +
    verbs permitted by :func:`_qa_command_allowed` ever run. This REPLACES the
    original (shell-based) ``execute_sync`` rather than delegating to it."""
    import subprocess as _subprocess

    from deile.tools.base import ToolResult

    def _readonly_exec(context):
        cmd = ((getattr(context, "parsed_args", None) or {}).get("command") or "")
        ok, reason = _qa_command_allowed(cmd)
        if not ok:
            return ToolResult.error_result(
                message=f"monitor-qa: comando recusado — {reason}.",
            )
        argv = shlex.split(cmd)
        try:
            proc = _subprocess.run(
                argv, shell=False, capture_output=True, text=True, timeout=60,
            )
        except _subprocess.TimeoutExpired:
            return ToolResult.error_result(message="monitor-qa: comando excedeu 60s.")
        except (OSError, ValueError) as exc:
            return ToolResult.error_result(message=f"monitor-qa: falha ao executar: {exc}")
        out = proc.stdout or ""
        if proc.returncode != 0:
            err = (proc.stderr or out or f"exit {proc.returncode}")[:2000]
            return ToolResult.error_result(message=f"comando retornou {proc.returncode}: {err}")
        return ToolResult.success_result(data=out[:20000], message=out[:200])

    bash_tool.execute_sync = _readonly_exec  # type: ignore[attr-defined]


def _install_monitor_qa_readonly_guard() -> None:
    """Harden the agent for read-only Q&A: drop mutating tools + guard bash.

    Mirrors :func:`_install_monitor_negative_whitelist` but is far stricter:
    it removes every tool that can change state (file writes, package install,
    code/test execution, dispatch, messaging) and wraps ``bash_execute`` with
    :func:`_wrap_bash_readonly`. Only read tools survive: ``bash_execute``
    (guarded), ``read_file``, ``list_files``, ``find_in_files``,
    ``vision_describe_image``.
    """
    import asyncio as _asyncio

    import deile.core.agent as agent_mod

    DROP = {
        "dispatch_deile_task", "dispatch_parallel_subagents",
        "write_file", "edit_file", "delete_file",
        "python_execute", "pip_install", "run_tests",
        "worktree", "pipeline", "pipeline_schedule",
        "cron_create", "cron_delete",
        "discord_send_message", "discord_send_dm", "discord_edit_message",
        "discord_react", "discord_start_thread", "discord_pin_message",
        "discord_mention_role", "discord_get_user_profile",
        "whatsapp_send_template",
    }
    original_init = agent_mod.DeileAgent.initialize

    async def _harden(self, *args, **kwargs):
        result = await original_init(self, *args, **kwargs)
        try:
            registry = self.tool_registry
            tools = registry.list_all()
        except Exception:  # noqa: BLE001 — never block startup over introspection
            return result
        dropped = []
        for tool in tools:
            if tool.name in DROP and registry.disable_tool(tool.name):
                dropped.append(tool.name)
        bash = registry.get("bash_execute")
        if bash is not None and hasattr(bash, "execute_sync"):
            _wrap_bash_readonly(bash)
            guarded = "bash_execute"
        else:
            guarded = "none"
        # Apply the read-only persona reliably: DEILE_DEFAULT_PERSONA is NOT
        # consumed anywhere in deile/ (the env var is dead); switch_persona is
        # the real mechanism. Safety does not depend on this — the guard above
        # enforces read-only regardless of which persona loads — but the
        # monitor_qa instructions improve answer quality.
        try:
            pm = getattr(self, "persona_manager", None)
            if pm is not None:
                await pm.switch_persona("monitor_qa")
        except Exception:  # noqa: BLE001 — guard already enforces read-only
            pass
        print(
            f"wrapper(monitor-qa): read-only guard active — "
            f"dropped={sorted(dropped)} guarded={guarded} persona=monitor_qa",
            file=sys.stderr,
        )
        return result

    if _asyncio.iscoroutinefunction(original_init):
        agent_mod.DeileAgent.initialize = _harden


def _run_monitor_qa(passthrough: List[str]) -> int:
    """deile-monitor-qa mode: one-shot READ-ONLY cluster/pipeline/forge Q&A.

    Invoked on demand by ``monitor_command_server`` (``POST /v1/ask``) as a
    subprocess inside the deile-monitor Pod — the only Pod with kubectl +
    forge + ``/state`` visibility. Mirrors :func:`_run_monitor` bootstrap but
    pins the read-only ``monitor_qa`` persona and installs
    :func:`_install_monitor_qa_readonly_guard`. The one-shot CLI prints the
    agent's final ``response.content`` to stdout — that is the answer the
    server returns.
    """
    _harden_runtime_dirs()
    loaded = _load_secret_files(Path("/run/secrets/deile"))
    if not _has_llm_key(loaded):
        print(
            "wrapper(monitor-qa): no *_API_KEY found under /run/secrets/deile — "
            "cannot bootstrap any LLM provider.",
            file=sys.stderr,
        )
        return 78  # EX_CONFIG

    # Forge token is OPTIONAL for Q&A: pure-k8s questions only need kubectl.
    # When present, wire git/gh/glab credentials so forge questions work too.
    has_forge = any(
        (name in loaded) or bool(os.environ.get(name))
        for name in ("GITHUB_TOKEN", "GITLAB_TOKEN", "GL_TOKEN")
    )
    if has_forge:
        _setup_forge_credentials()
    else:
        print(
            "wrapper(monitor-qa): no forge token under /run/secrets/deile — "
            "forge-scoped questions will be limited to kubectl/state.",
            file=sys.stderr,
        )

    _patch_deile_bootstrap()

    # kubectl in this Pod only authenticates via an explicit kubeconfig (issue
    # #504): build one and point KUBECONFIG at it so the allow-listed `kubectl`
    # reads work in Q&A. Best-effort — forge/state questions still answer without
    # it. (The Phase-B/tick persona never ran kubectl, so this is net-new here.)
    try:
        import tempfile

        import monitor_core  # sibling in /app
        _kc = os.path.join(tempfile.gettempdir(), "deile-monitor-qa-kubeconfig")
        if monitor_core.resolve_incluster_kube(monitor_core.run_cmd, _kc):
            os.environ["KUBECONFIG"] = _kc
    except Exception as exc:  # noqa: BLE001 — kubectl-less Q&A still works
        print(f"wrapper(monitor-qa): kubeconfig setup skipped: {exc}", file=sys.stderr)

    try:
        _install_monitor_qa_readonly_guard()
    except Exception as exc:  # noqa: BLE001 — refuse to start unsafe
        print(f"wrapper(monitor-qa): read-only guard install failed: {exc}", file=sys.stderr)
        return 78

    # Persona is applied by the guard's switch_persona("monitor_qa") after init
    # (DEILE_DEFAULT_PERSONA is dead code). The one-shot CLI prints the agent's
    # final response.content to stdout — that is the answer the server returns.
    sys.argv = ["deile", *passthrough]
    from deile.cli import main as deile_main
    return deile_main() or 0


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: wrapper.py {deile|bot|worker|claude-worker|cli-worker|"
            "pipeline|monitor|monitor-qa} <args ...>",
            file=sys.stderr,
        )
        return 64  # EX_USAGE
    role, rest = argv[1], argv[2:]
    if role == "deile":
        return _run_deile(rest)
    if role == "bot":
        return _run_bot(rest)
    if role == "worker":
        return _run_worker(rest)
    if role == "claude-worker":
        return _run_claude_worker(rest)
    if role == "cli-worker":
        return _run_cli_worker(rest)
    if role == "pipeline":
        return _run_pipeline(rest)
    if role == "monitor":
        return _run_monitor(rest)
    if role == "monitor-qa":
        return _run_monitor_qa(rest)
    print(
        f"wrapper: unknown role {role!r} "
        "(expected 'deile' | 'bot' | 'worker' | 'claude-worker' | 'cli-worker' | "
        "'pipeline' | 'monitor' | 'monitor-qa')",
        file=sys.stderr,
    )
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv))
