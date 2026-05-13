"""Path resolution + post-write validation hints for `file_tools`.

Two concerns colocated because both are pure helpers that operate on
LLM-supplied path strings without any tool execution context:

1. Path normalization: turn the noisy strings LLMs produce
   (``@foo``, ``C:\\bar``, ``/etc/...``, ``~/baz``) into safe project-
   relative paths inside the working directory. See ``_resolve_project_path``.

2. Post-write validation hints: given a freshly written file, what cheap
   command should the LLM run to verify it parses / compiles? See
   ``_post_write_validation_hint`` and ``_apply_post_write_hint``.

Extracted from `file_tools.py` (formerly 1813 LOC, MI 0.00) to keep that
file focused on the five `SyncTool` classes (Read/Write/Edit/List/Delete).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.exceptions import ValidationError

logger = logging.getLogger(__name__)


# Extension → cheap, side-effect-free validator the LLM should run after a write.
_POST_WRITE_VALIDATORS: Dict[str, Dict[str, str]] = {
    ".py":  {"kind": "python_syntax",  "template": "python -m py_compile {path}"},
    ".sh":  {"kind": "bash_syntax",    "template": "bash -n {path}"},
    ".json": {"kind": "json_parse",    "template": 'python -c "import json; json.load(open({path!r}))"'},
    ".yaml": {"kind": "yaml_parse",    "template": 'python -c "import yaml; yaml.safe_load(open({path!r}))"'},
    ".yml":  {"kind": "yaml_parse",    "template": 'python -c "import yaml; yaml.safe_load(open({path!r}))"'},
    ".js":  {"kind": "node_syntax",    "template": "node --check {path}"},
    ".mjs": {"kind": "node_syntax",    "template": "node --check {path}"},
    ".ts":  {"kind": "typescript_check", "template": "npx --yes tsc --noEmit {path}"},
    ".tsx": {"kind": "typescript_check", "template": "npx --yes tsc --noEmit --jsx react {path}"},
}


def _post_write_validation_hint(file_path: str) -> Optional[Dict[str, str]]:
    """Return a validation hint for files whose extension is executable / parseable.

    Returns None for extensions we don't have a cheap validator for (text,
    markdown, etc.) — the persona's DoD still applies for those, but there
    is no specific shell command to suggest.
    """
    suffix = Path(file_path).suffix.lower()
    spec = _POST_WRITE_VALIDATORS.get(suffix)
    if spec is None:
        return None
    return {"kind": spec["kind"], "command": spec["template"].format(path=file_path)}


def _apply_post_write_hint(
    relative_path: str,
    metadata: Dict[str, Any],
    message_parts: List[str],
) -> None:
    """Mutate `metadata` and `message_parts` in-place with a post-write hint.

    Shared between Write/Edit tools; the prior duplicated block (~13 lines
    each) was the same in both call sites.
    """
    hint = _post_write_validation_hint(relative_path)
    if hint is None:
        return
    metadata["post_write_validation_required"] = True
    metadata["post_write_validation_command"] = hint["command"]
    metadata["post_write_validation_kind"] = hint["kind"]
    message_parts.append(
        f"\n⚠️  POST_WRITE_VALIDATION_REQUIRED: per the Definition of Done, "
        f"your next action MUST validate this file. Suggested command:\n"
        f"    {hint['command']}\n"
        f"Do NOT declare the task complete until validation succeeds "
        f"(exit 0) or you have explicitly diagnosed and reported a "
        f"failure to the user."
    )


class LocalFileAccessViolation(ValidationError):
    """Exceção para violações de acesso a arquivos locais"""
    pass


@dataclass(frozen=True)
class ResolvedPath:
    """Output of :func:`_resolve_project_path`.

    Attributes
    ----------
    absolute:
        Final resolved absolute path string. Always inside the working
        directory.
    relative_to_cwd:
        Path relative to the working directory (POSIX-style separators), used
        for human-facing display and tool result messages.
    input:
        The exact string the caller passed in, preserved for diagnostic
        messages.
    note:
        ``None`` when the input was already a clean project-relative path.
        A human-readable string describing the normalization when one
        happened (e.g. ``"leading '/' stripped — interpreted as project-relative"``).
        The note flows into ``write_file``'s ``message`` so the LLM sees
        exactly what the system did with its input and can correct course
        on the next turn instead of misremembering where the file landed.
    """

    absolute: str
    relative_to_cwd: str
    input: str
    note: Optional[str]


# Patterns we reject outright, even after normalization.
# `<>|*?` are shell metachars that don't belong in well-formed paths.
# Null byte is a classic path-injection vector in C-extension callers.
_DANGEROUS_PATH_CHARS = re.compile(r"[\x00<>|*?]")

# Windows drive prefix: ``C:\foo``, ``D:/bar``, ``c:\\baz``, etc.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]+")


def _looks_like_outside_project(path: Optional[str]) -> bool:
    """Heuristic: did the LLM attempt a path that clearly targets outside the
    project working directory?

    Used to decide whether ``Path not found`` errors should include a hint
    pointing at ``bash_execute`` (which has no working-directory sandbox).
    Triggers on:

    * Leading ``/`` (system-absolute, e.g. ``/Users/...``, ``/etc/...``).
    * Leading ``..`` (parent-relative, e.g. ``../sibling/file``).
    * Leading ``~`` (home, e.g. ``~/foo`` — note: file_tools does NOT expand
      ``~``; we still treat it as a clear "outside" intent).

    Pure-string check, no I/O. Conservative on purpose — false positives just
    add a helpful hint to a real error message, never block legitimate paths.
    """
    if not isinstance(path, str):
        return False
    stripped = path.strip()
    if not stripped:
        return False
    if stripped.startswith("/"):
        return True
    if stripped.startswith("..") and (len(stripped) == 2 or stripped[2] in ("/", "\\")):
        return True
    if stripped.startswith("~"):
        return True
    return False


def _resolve_absolute_or_strip(
    candidate: str, work_dir: Path, raw: str
) -> Tuple[str, Optional[Path], Optional[str]]:
    """Handle a path that starts with '/'. Returns ``(candidate, target, note)``:

    - If the path resolves inside ``work_dir``, keep it as-is — ``target`` is
      the resolved Path, no note.
    - Otherwise the leading '/' is stripped and the caller will resolve the
      remainder against ``work_dir`` — ``target`` is None, note describes
      the normalization.
    """
    try:
        as_is = Path(candidate).resolve()
    except (OSError, RuntimeError):
        as_is = None
    if as_is is not None:
        try:
            as_is.relative_to(work_dir)
            return candidate, as_is, None
        except ValueError:
            pass
    stripped = candidate.lstrip("/")
    if not stripped:
        raise LocalFileAccessViolation(
            f"path is just slashes: {raw!r} — refuse to write to the project "
            "root as a file"
        )
    return stripped, None, (
        "leading '/' stripped — interpreted as project-relative, "
        "NOT as a system-absolute path. The file lives INSIDE the "
        "project working directory."
    )


def _resolve_project_path(file_path: str, working_directory: str) -> ResolvedPath:
    """Resolve an LLM-supplied path to an absolute path inside ``working_directory``.

    Normalizes the patterns LLMs mangle: ``@`` prefix, backslashes, Windows
    drives, ``~``/``~/`` (NOT expanded to ``$HOME`` — treated as project-
    relative), and leading ``/`` (treated as project-relative typo unless
    the absolute path is already inside CWD). ``..`` is allowed when it
    resolves inside CWD; rejected when it escapes.
    """
    if file_path is None:
        raise LocalFileAccessViolation("path is None")

    raw = file_path
    if not isinstance(raw, str):
        raise LocalFileAccessViolation(
            f"path must be str, got {type(raw).__name__}"
        )

    stripped = raw.strip()
    if not stripped:
        raise LocalFileAccessViolation("path is empty")

    if _DANGEROUS_PATH_CHARS.search(stripped):
        raise LocalFileAccessViolation(
            f"path contains forbidden characters (null byte or shell "
            f"metacharacters <>|*?): {raw!r}"
        )

    notes: List[str] = []
    candidate = stripped

    # 2. @-prefix
    if candidate.startswith("@"):
        candidate = candidate[1:]
        notes.append("'@' prefix stripped")

    # 3. Backslash → forward slash
    if "\\" in candidate:
        candidate = candidate.replace("\\", "/")
        notes.append("backslashes converted to forward slashes")

    # 4. Windows drive prefix
    if _WINDOWS_DRIVE_RE.match(candidate):
        candidate = _WINDOWS_DRIVE_RE.sub("", candidate)
        notes.append("Windows drive prefix stripped — path is project-relative")

    # 5. Home shorthand
    if candidate.startswith("~/") or candidate == "~":
        candidate = candidate[2:] if candidate.startswith("~/") else ""
        notes.append(
            "leading '~' stripped — '~' is NOT expanded to system $HOME; "
            "path is project-relative"
        )
        if not candidate:
            candidate = "."

    # 6. Leading slash. Three cases handled by _resolve_absolute_or_strip:
    # (a) absolute and already inside CWD → pass through; (b) absolute and
    # outside → strip the slash and treat as project-relative typo; (c)
    # unresolvable → strip and let containment check catch escapes.
    work_dir = Path(working_directory).resolve()
    target: Optional[Path] = None
    if candidate.startswith("/"):
        candidate, target, slash_note = _resolve_absolute_or_strip(candidate, work_dir, raw)
        if slash_note:
            notes.append(slash_note)

    if not candidate:
        candidate = "."

    if target is None:
        try:
            target = (work_dir / candidate).resolve()
        except (OSError, RuntimeError) as exc:
            raise LocalFileAccessViolation(
                f"could not resolve {raw!r} against {work_dir}: {exc}"
            ) from exc

    # Final containment check. Path.is_relative_to was added in 3.9; we
    # support older runtimes via try/relative_to.
    try:
        target.relative_to(work_dir)
    except ValueError:
        raise LocalFileAccessViolation(
            f"path {raw!r} resolves to {target}, which is OUTSIDE the project "
            f"working directory {work_dir}. Use a project-relative path "
            f"(e.g. drop any leading '..' that escapes the project root). "
            f"For files OUTSIDE the project (parent repo, sibling project, "
            f"system paths like /etc/), use `bash_execute` (e.g. "
            f"`ls {target}` or `cat {target}`) — bash_execute has no "
            f"working-directory sandbox."
        )

    # POSIX-style relative for display (works across platforms in messages)
    rel = target.relative_to(work_dir).as_posix() or "."
    note = "; ".join(notes) if notes else None

    if note is not None:
        logger.debug(
            "path normalized: input=%r resolved=%s note=%s", raw, target, note
        )

    return ResolvedPath(
        absolute=str(target),
        relative_to_cwd=rel,
        input=raw,
        note=note,
    )


def _validate_path_within_working_directory(file_path: str, working_directory: str) -> str:
    """Backward-compatible wrapper returning only the absolute path.

    Existing callers that only need the path string can keep using this. New
    code should call :func:`_resolve_project_path` directly to get access to
    the normalization ``note`` and surface it to the LLM.
    """
    return _resolve_project_path(file_path, working_directory).absolute
