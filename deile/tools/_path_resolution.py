"""Path resolution + post-write validation hints for `file_tools`.

Four concerns colocated because all are pure helpers that operate on
LLM-supplied path strings without any tool execution context:

1. Path normalization: turn the noisy strings LLMs produce
   (``@foo``, ``C:\\bar``, ``/etc/...``, ``~/baz``) into safe project-
   relative paths inside the working directory. See ``_resolve_project_path``.

2. Post-write validation hints: given a freshly written file, what cheap
   command should the LLM run to verify it parses / compiles? See
   ``_post_write_validation_hint`` and ``_apply_post_write_hint``.

3. Path-argument extraction: pick the target path out of ``parsed_args``
   using each tool's canonical synonym precedence. See ``_extract_path_arg``
   and the per-tool ``_PATH_ARG_KEYS_*`` tuples below.

4. File-not-found message composition: build the shared "File not found"
   text that Read/Edit/Delete return when the resolved path doesn't exist
   — including the optional ``bash_execute`` escape hatch for paths that
   clearly target outside the project. See ``_not_found_message``.

Extracted from `file_tools.py` (formerly 1813 LOC, MI 0.00) to keep that
file focused on the five `SyncTool` classes (Read/Write/Edit/List/Delete).
"""

from __future__ import annotations

import logging
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..core.exceptions import ValidationError

logger = logging.getLogger(__name__)


# Short launcher for the Python that's running DEILE — picked per-platform so
# the hint actually works on the operator's machine:
#   - macOS / Linux: ``python3`` (stock macOS has no unversioned ``python``;
#     modern distros increasingly drop it too).
#   - Windows: ``python`` (the python.org installer adds ``python.exe`` to
#     PATH; ``python3`` only exists if you installed via the Windows Store).
# Avoids the "python: command not found" → retry loop that costs an extra
# round-trip and a validation-gate panel to recover from a typo we'd have put
# in the hint ourselves.
_PYTHON_LAUNCHER = "python" if sys.platform == "win32" else "python3"


# Extension → cheap, side-effect-free validator the LLM should run after a write.
_POST_WRITE_VALIDATORS: Dict[str, Dict[str, str]] = {
    ".py": {
        "kind": "python_syntax",
        "template": f"{_PYTHON_LAUNCHER} -m py_compile {{path}}",
    },
    ".sh": {"kind": "bash_syntax", "template": "bash -n {path}"},
    ".json": {
        "kind": "json_parse",
        "template": f'{_PYTHON_LAUNCHER} -c "import json; json.load(open({{path!r}}))"',
    },
    ".yaml": {
        "kind": "yaml_parse",
        "template": f'{_PYTHON_LAUNCHER} -c "import yaml; yaml.safe_load(open({{path!r}}))"',
    },
    ".yml": {
        "kind": "yaml_parse",
        "template": f'{_PYTHON_LAUNCHER} -c "import yaml; yaml.safe_load(open({{path!r}}))"',
    },
    ".js": {"kind": "node_syntax", "template": "node --check {path}"},
    ".mjs": {"kind": "node_syntax", "template": "node --check {path}"},
    ".ts": {"kind": "typescript_check", "template": "npx --yes tsc --noEmit {path}"},
    ".tsx": {
        "kind": "typescript_check",
        "template": "npx --yes tsc --noEmit --jsx react {path}",
    },
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
    return (
        stripped,
        None,
        (
            "leading '/' stripped — interpreted as project-relative, "
            "NOT as a system-absolute path. The file lives INSIDE the "
            "project working directory."
        ),
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
        raise LocalFileAccessViolation(f"path must be str, got {type(raw).__name__}")

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
        candidate, target, slash_note = _resolve_absolute_or_strip(
            candidate, work_dir, raw
        )
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
        # Shell-escape ``target`` for the same reason ``_not_found_message``
        # quotes its absolute path: this message is rendered back to the
        # LLM, which may copy the ``ls``/``cat`` suggestion verbatim into a
        # follow-up ``bash_execute`` call. A path containing ``;``, ``&``,
        # ``$``, backticks, spaces or quotes would otherwise fabricate a
        # shell command at execution time.
        target_quoted = shlex.quote(str(target))
        raise LocalFileAccessViolation(
            f"path {raw!r} resolves to {target}, which is OUTSIDE the project "
            f"working directory {work_dir}. Use a project-relative path "
            f"(e.g. drop any leading '..' that escapes the project root). "
            f"For files OUTSIDE the project (parent repo, sibling project, "
            f"system paths like /etc/), use `bash_execute` (e.g. "
            f"`ls {target_quoted}` or `cat {target_quoted}`) — bash_execute "
            f"has no working-directory sandbox."
        )

    # POSIX-style relative for display (works across platforms in messages)
    rel = target.relative_to(work_dir).as_posix() or "."
    note = "; ".join(notes) if notes else None

    if note is not None:
        logger.debug("path normalized: input=%r resolved=%s note=%s", raw, target, note)

    return ResolvedPath(
        absolute=str(target),
        relative_to_cwd=rel,
        input=raw,
        note=note,
    )


def _validate_path_within_working_directory(
    file_path: str, working_directory: str
) -> str:
    """Backward-compatible wrapper returning only the absolute path.

    Existing callers that only need the path string can keep using this. New
    code should call :func:`_resolve_project_path` directly to get access to
    the normalization ``note`` and surface it to the LLM.
    """
    return _resolve_project_path(file_path, working_directory).absolute


# Argument keys the LLM may use for the target path. ``file_path`` is the
# documented schema key; the rest are defensive fallbacks for when the model
# passes a near-miss synonym. Each tool has historically used a slightly
# different ordering — the per-tool tuples below pin the original behavior
# so a single centralized helper doesn't silently change precedence.
#
# WriteFileTool original order (pre-DRY refactor): file_path > filename >
# path > file > filepath. Other tools share file_path/path as the canonical
# primary pair; ReadFileTool and DeleteFileTool further split the lookup
# around a file_list fallback (see two-stage callsites in file_tools.py).
_PATH_ARG_KEYS_PRIMARY: Tuple[str, ...] = ("file_path", "path")
_PATH_ARG_KEYS_FALLBACK: Tuple[str, ...] = ("file", "filename", "filepath")
_PATH_ARG_KEYS_WRITE: Tuple[str, ...] = (
    "file_path",
    "filename",
    "path",
    "file",
    "filepath",
)
_PATH_ARG_KEYS_EDIT: Tuple[str, ...] = (
    "file_path",
    "path",
    "filename",
    "file",
    "filepath",
)
# Default precedence kept for backwards-compatible callers (and matches
# EditFileTool's historical order, which is the same as the canonical
# "file_path first, all synonyms after" sequence).
_PATH_ARG_KEYS: Tuple[str, ...] = _PATH_ARG_KEYS_EDIT


def _extract_path_arg(
    parsed_args: Dict[str, Any],
    keys: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Return the first non-empty string path argument from ``parsed_args``.

    Each file tool passes its own canonical precedence tuple via ``keys``;
    a missing ``keys`` falls back to ``_PATH_ARG_KEYS`` (full synonym set)
    for legacy/test callers. Only ``str`` values count — the previous
    truthy-check accidentally accepted lists, dicts and ints when the LLM
    sent a malformed payload. Returns ``None`` when no path-like key
    carries a non-empty string; the caller's remaining fallbacks
    (file_list, positional args, user_input) take over from there.
    """
    selected = tuple(keys) if keys is not None else _PATH_ARG_KEYS
    for key in selected:
        value = parsed_args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _not_found_message(
    resolved: ResolvedPath,
    original_input: Optional[str],
    *,
    detail: str = "",
    include_bash_hint: bool = False,
    bash_verb: str = "cat",
) -> str:
    """Compose the canonical "file not found" message for the file tools.

    Read/Edit/Delete each rebuilt this message near-identically: the
    normalization hint (``input was ... → note``) plus an optional
    ``bash_execute`` escape hatch for paths that clearly target outside
    the project. ``detail`` carries the tool-specific middle sentence
    (e.g. Read's "use list_files", Edit's "use write_file"); ``bash_verb``
    is the command shown in the bash hint (``cat`` for read, ``rm`` for
    delete). The exact wording is load-bearing — it steers the LLM out of
    retry loops — so keep it stable when editing.
    """
    norm_hint = (
        f" (input was {resolved.input!r} → {resolved.note})" if resolved.note else ""
    )
    bash_hint = ""
    if include_bash_hint and (
        resolved.note or _looks_like_outside_project(original_input)
    ):
        # Shell-escape the resolved absolute path so paths containing ``;``,
        # ``&``, ``$``, backticks, spaces or quotes can't fabricate a shell
        # command injection inside the hint string the LLM is shown.
        quoted_path = shlex.quote(str(resolved.absolute))
        bash_hint = (
            " If the file lives OUTSIDE the project, use "
            f'bash_execute(command="{bash_verb} {quoted_path}") instead — '
            "bash_execute has no working-directory sandbox."
        )
    detail_part = f" {detail}" if detail else ""
    return (
        f"File not found: {resolved.relative_to_cwd}{norm_hint}."
        f"{detail_part}{bash_hint}"
    )
