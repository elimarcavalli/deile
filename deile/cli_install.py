"""Self-install helpers for the ``deile`` console script.

Extracted from ``deile/cli.py`` to keep the CLI entry point focused on
``main()`` / ``_DeileCLI`` orchestration. The functions here implement the
``deile --install`` flow: pick a target directory, create an isolated venv,
install DEILE + its frozen requirements with ``pip``, drop a thin shim onto
``PATH`` (POSIX symlink or Windows ``.cmd``), and optionally append a
``PATH`` export to the user's shell rc file.

All functions take explicit arguments so they remain unit-testable without
spinning up the rest of the CLI. The public synchronous entry point is
``_run_self_install`` (kept as private-prefixed name because ``cli.py`` is
the only intended caller — re-imports it as a local symbol).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import venv as _venv  # noqa: N812 — local alias for testability (patched as deile.cli_install._venv)
from pathlib import Path
from typing import Optional

# ── package root (where deile/ lives) ───────────────────────────────────────
# Locally derived so this module does not depend on ``cli.py`` (avoids an
# import cycle: ``cli`` imports ``cli_install`` at module load).
_PACKAGE_ROOT = Path(__file__).parent.resolve()
_PROJECT_ROOT = _PACKAGE_ROOT.parent  # repo root when editable, same when installed

# ── install helpers — module-level constants ─────────────────────────────────
_KNOWN_SHELLS = frozenset({"zsh", "bash", "fish"})


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
