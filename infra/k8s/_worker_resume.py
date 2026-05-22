#!/usr/bin/env python3
"""Resume-mode helpers for the deile-worker (issue #254).

The autonomous pipeline can no longer afford to throw away partial work when a
single dispatch runs out of tool-call rounds, times out, or the agent declares
it is not done. This module is the worker-side machinery that makes a dispatch
*resumable*:

- **State files** live in the per-channel PVC workspace, never inside the git
  clone, so they never enter a commit/PR:
    - ``.deile-progress.md``   — the human-readable journal (what I did / what
      is left / key decisions / blockers). The agent writes it on pause; if it
      crashes/times-out without writing, the worker auto-summarizes the final
      transcript as a fallback (hybrid strategy, item 3 of the spec).
    - ``.deile-progress.json`` — ``{tentativa, fingerprint_substantivo_anterior,
      budget_acumulado_s}`` for the progress guard (item 4) and the ceiling
      (item 6).
- **Ground-truth-first end detection** (item 5): the pipeline must decide
  CONCLUÍDO / INCOMPLETO / BLOQUEADO from the *real* git/PR state, not from the
  model obeying an output format. The single thing that comes from the agent is
  an explicit ``BLOQUEADO: <motivo>`` line (only it knows a hard impediment).
- **Substantive fingerprint** (item 4): a stable hash of the tracked diff +
  untracked files, EXCLUDING ``.deile-progress.*`` and other meta files, so the
  guard compares real code/test changes between attempts.

Everything here is pure logic plus ``git`` subprocess calls — no aiohttp, no
agent — so it is unit-testable without the full worker stack. ``worker_server``
imports and orchestrates these helpers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("deile.worker_server.resume")

# ---- State-file constants ----------------------------------------------------

#: Human-readable journal written on pause (agent) or summarized (worker).
PROGRESS_MD = ".deile-progress.md"
#: Machine state for the progress guard + attempt/budget ceiling.
PROGRESS_JSON = ".deile-progress.json"
#: Workspace-local gitignore inside the clone, ensuring the state files (and
#: their siblings) NEVER enter a commit/PR even if they are written into the
#: repo working tree by mistake.
WORKSPACE_GITIGNORE = ".git/info/exclude"

#: Meta files excluded from the substantive fingerprint (item 4). Matching is
#: by exact path or basename so a relocation of the journal still excludes it.
META_FILES = frozenset({PROGRESS_MD, PROGRESS_JSON})
_META_BASENAMES = frozenset({PROGRESS_MD, PROGRESS_JSON, ".deile-progress"})

#: The agent declares a hard impediment with a line starting with this token
#: (case-insensitive, optional leading markdown/whitespace). Only the agent can
#: know a true block, so this is the one signal that comes from the model.
_BLOCKED_RE = re.compile(r"^\s*[>*\-\s]*BLOQUEAD[OA]\s*[:\-]\s*(.+)$", re.IGNORECASE | re.MULTILINE)

#: A confirmed PR URL anywhere in the agent transcript (ground-truth fallback
#: when ``gh`` cannot be queried). The pipeline ALSO cross-checks the real PR
#: state, so this is a hint, not the sole source.
_PR_URL_RE = re.compile(r"https://github\.com/[^\s\"'<>]+/pull/\d+", re.IGNORECASE)

# How a dispatch ended, from the worker's point of view. The pipeline maps
# these to label transitions; the value is also stored in the result.
ENDED_CONCLUIDO = "concluido"
ENDED_INCOMPLETO = "incompleto"
ENDED_BLOQUEADO = "bloqueado"

# Why the agent's tool-loop ended (best-effort, for diagnostics + the journal).
LOOP_TIMEOUT = "timeout"
LOOP_CAP = "cap_iteracoes"
LOOP_NATURAL = "natural"
LOOP_ERROR = "erro"


# ---- Workspace / clone path helpers ------------------------------------------

def repo_dir(workdir: Path) -> Path:
    """Return the git clone path inside *workdir* (the ``./repo`` convention)."""
    return Path(workdir) / "repo"


def _is_git_repo(repo: Path) -> bool:
    return (repo / ".git").exists()


# ---- git plumbing (sync; the worker calls these under its task lock) ----------

def _git(repo: Path, *args: str, timeout: float = 30.0) -> Tuple[int, str, str]:
    """Run ``git`` inside *repo*; return ``(rc, stdout, stderr)``.

    Never raises on a non-zero exit — callers decide what an error means. A
    missing ``git`` binary or a timeout is surfaced as ``rc != 0`` with the
    reason in stderr so the worker degrades to "no ground truth available"
    rather than crashing the dispatch.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        logger.warning("git %s in %s failed: %s", " ".join(args), repo, exc)
        return 1, "", str(exc)


def current_branch(repo: Path) -> str:
    """Return the checked-out branch name (empty string if undetermined)."""
    if not _is_git_repo(repo):
        return ""
    rc, out, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else ""


def collect_diff(repo: Path, main_branch: str = "main") -> str:
    """Return the unified diff of the branch vs *main_branch* (tracked changes).

    Uses ``git diff <main>...HEAD`` plus the working-tree diff so uncommitted
    edits are captured too. Empty string when the repo is absent or git fails.
    """
    if not _is_git_repo(repo):
        return ""
    parts: List[str] = []
    rc, out, _ = _git(repo, "diff", f"{main_branch}...HEAD")
    if rc == 0 and out.strip():
        parts.append(out)
    # Uncommitted (staged + unstaged) changes on top of HEAD.
    rc, out, _ = _git(repo, "diff", "HEAD")
    if rc == 0 and out.strip():
        parts.append(out)
    return "\n".join(parts)


def list_untracked(repo: Path) -> List[str]:
    """Return untracked file paths (relative to repo root), excluding ignored.

    The workspace-local exclude (``.git/info/exclude``) already hides the
    ``.deile-progress.*`` files from git, so they will not appear here — but we
    still filter meta basenames defensively in :func:`_is_meta`.
    """
    if not _is_git_repo(repo):
        return []
    rc, out, _ = _git(repo, "ls-files", "--others", "--exclude-standard")
    if rc != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _is_meta(rel_path: str) -> bool:
    """True if *rel_path* is a resume meta file excluded from the fingerprint."""
    name = Path(rel_path).name
    return rel_path in META_FILES or name in _META_BASENAMES


def _substantive_diff_lines(diff: str) -> List[str]:
    """Filter a unified diff down to substantive +/- content lines.

    Drops diff headers (``diff --git``, ``index``, ``+++``/``---`` file markers,
    ``@@`` hunk headers) AND any hunk belonging to a meta file, so a change that
    touches ONLY ``.deile-progress.*`` produces an empty substantive set (0
    progress). Keeps real added/removed code/test lines.
    """
    keep: List[str] = []
    skip_file = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            # ``diff --git a/<path> b/<path>`` — decide if this file is meta.
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            skip_file = bool(m and (_is_meta(m.group(1)) or _is_meta(m.group(2))))
            continue
        if skip_file:
            continue
        if line.startswith(("index ", "--- ", "+++ ", "@@", "new file mode",
                            "deleted file mode", "similarity index", "rename ")):
            continue
        if line.startswith(("+", "-")):
            keep.append(line.rstrip())
    return keep


def compute_fingerprint(
    repo: Path,
    untracked_contents: Optional[Dict[str, str]] = None,
    *,
    main_branch: str = "main",
) -> str:
    """Compute the SUBSTANTIVE-change fingerprint of the workspace (item 4).

    The fingerprint is a sha256 over:
      - the substantive lines of ``git diff <main>...HEAD`` + working-tree diff
        (meta-file hunks excluded), and
      - the sorted ``(path, sha256(content))`` of every non-meta untracked file.

    Two attempts that produce byte-identical substantive code/test changes get
    the SAME fingerprint → the progress guard reads that as 0 progress. Changes
    confined to ``.deile-progress.*`` do not move the fingerprint.

    ``untracked_contents`` lets the caller (and tests) inject already-read file
    bodies; when omitted, untracked files are read from disk here.
    """
    diff = collect_diff(repo, main_branch)
    substantive = _substantive_diff_lines(diff)

    untracked = list_untracked(repo)
    items: List[str] = []
    for rel in sorted(untracked):
        if _is_meta(rel):
            continue
        if untracked_contents is not None and rel in untracked_contents:
            content = untracked_contents[rel]
        else:
            try:
                content = (repo / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
        digest = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()
        items.append(f"{rel}:{digest}")

    h = hashlib.sha256()
    h.update("\n".join(substantive).encode("utf-8", errors="replace"))
    h.update(b"\x00UNTRACKED\x00")
    h.update("\n".join(items).encode("utf-8", errors="replace"))
    return h.hexdigest()


def has_substantive_work(repo: Path, *, main_branch: str = "main") -> bool:
    """True if there is ANY substantive (non-meta) change in the workspace.

    Used by ground-truth end detection: an attempt that produced no PR but DID
    leave real code/test changes is INCOMPLETO (resumable), whereas one that
    left nothing substantive is a candidate for the progress guard / block.
    """
    diff = collect_diff(repo, main_branch)
    if _substantive_diff_lines(diff):
        return True
    return any(not _is_meta(rel) for rel in list_untracked(repo))


# ---- gitignore / state-file safety -------------------------------------------

def ensure_state_files_ignored(repo: Path) -> None:
    """Append the resume state files to the clone's ``.git/info/exclude``.

    ``.git/info/exclude`` is a workspace-local ignore that is NOT committed and
    NOT shared — perfect for guaranteeing ``.deile-progress.*`` never enter a
    commit/PR (item: state files never in the PR) without touching the repo's
    tracked ``.gitignore``. Idempotent.
    """
    if not _is_git_repo(repo):
        return
    exclude_path = repo / WORKSPACE_GITIGNORE
    entries = [PROGRESS_MD, PROGRESS_JSON]
    try:
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        present = {line.strip() for line in existing.splitlines()}
        missing = [e for e in entries if e not in present]
        if not missing:
            return
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        block = ("" if existing.endswith("\n") or not existing else "\n")
        block += "# deile resume state (issue #254) — never commit\n"
        block += "\n".join(missing) + "\n"
        with open(exclude_path, "a", encoding="utf-8") as fh:
            fh.write(block)
    except OSError as exc:  # noqa: BLE001 — best-effort hardening
        logger.warning("could not update %s: %s", exclude_path, exc)


def strip_state_files_from_index(repo: Path) -> None:
    """Defensively un-stage the state files if the agent ``git add``ed them.

    Belt-and-suspenders with :func:`ensure_state_files_ignored`: if the agent
    force-added ``.deile-progress.*`` (``git add -f``), this removes them from
    the index (keeping the working-tree copy) so the next commit cannot carry
    them into the PR. No-op when they are not tracked.
    """
    if not _is_git_repo(repo):
        return
    for name in (PROGRESS_MD, PROGRESS_JSON):
        rc, out, _ = _git(repo, "ls-files", "--error-unmatch", name)
        if rc == 0 and out.strip():
            _git(repo, "rm", "--cached", "--quiet", name)


# ---- progress journal (.deile-progress.md) -----------------------------------

def progress_md_path(workdir: Path) -> Path:
    return Path(workdir) / PROGRESS_MD


def progress_json_path(workdir: Path) -> Path:
    return Path(workdir) / PROGRESS_JSON


def read_progress_md(workdir: Path) -> str:
    """Return the journal text, or empty string if absent/unreadable."""
    p = progress_md_path(workdir)
    try:
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except OSError:
        return ""


def write_progress_md(workdir: Path, text: str) -> None:
    """Write the journal to the workspace (overwrites)."""
    try:
        progress_md_path(workdir).write_text(text, encoding="utf-8")
    except OSError as exc:  # noqa: BLE001
        logger.warning("could not write %s: %s", PROGRESS_MD, exc)


def agent_wrote_progress(workdir: Path) -> bool:
    """True if a non-empty ``.deile-progress.md`` exists (agent wrote it)."""
    return bool(read_progress_md(workdir).strip())


def summarize_transcript_fallback(
    transcript: str,
    *,
    ended: str,
    motivo_fim_loop: str,
    pr_url: str = "",
    attempt: int = 1,
    max_chars: int = 4000,
) -> str:
    """Build a fallback journal from the final transcript (hybrid item 3).

    When the agent crashes/times-out without writing ``.deile-progress.md``,
    the worker synthesizes one from the tail of the transcript so the NEXT
    attempt still has continuity. Deliberately simple (no LLM): the tail of the
    transcript is the most recent state, which is exactly what the resume brief
    needs alongside the git diff.
    """
    tail = (transcript or "").strip()[-max_chars:]
    lines = [
        "# .deile-progress.md (resumo automático do worker)",
        "",
        f"> Gerado pelo worker como *fallback* — o agente não escreveu o journal "
        f"nesta tentativa (#{attempt}).",
        "",
        f"- **Como terminou**: {ended}",
        f"- **Motivo do fim do loop**: {motivo_fim_loop}",
    ]
    if pr_url:
        lines.append(f"- **PR detectada**: {pr_url}")
    lines += [
        "",
        "## Trecho final do transcript (estado mais recente)",
        "",
        "```text",
        tail or "(transcript vazio)",
        "```",
        "",
        "## O que falta",
        "",
        "- Continuar de onde o transcript acima parou; cruze com o `git diff` e "
        "os arquivos untracked já presentes no workspace.",
    ]
    return "\n".join(lines)


# ---- progress.json (attempt counter + fingerprint + budget) ------------------

def read_progress_state(workdir: Path) -> Dict[str, Any]:
    """Load ``.deile-progress.json``; return ``{}`` if absent/corrupt.

    Shape: ``{"tentativa": int, "fingerprint": str, "budget_acumulado_s": float}``.
    """
    p = progress_json_path(workdir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_progress_state(
    workdir: Path,
    *,
    attempt: int,
    fingerprint: str,
    budget_acumulado_s: float,
) -> None:
    """Persist the machine state for the progress guard + ceiling."""
    state = {
        "tentativa": int(attempt),
        "fingerprint": str(fingerprint),
        "budget_acumulado_s": float(budget_acumulado_s),
    }
    try:
        progress_json_path(workdir).write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:  # noqa: BLE001
        logger.warning("could not write %s: %s", PROGRESS_JSON, exc)


# ---- agent signal parsing ----------------------------------------------------

def extract_blocked_reason(transcript: str) -> Optional[str]:
    """Return the agent-declared block reason, or None.

    The agent signals a hard impediment with ``BLOQUEADO: <motivo>``. Only the
    LAST occurrence is honored (the agent may quote the instruction earlier).
    """
    if not transcript:
        return None
    matches = _BLOCKED_RE.findall(transcript)
    if not matches:
        return None
    reason = matches[-1].strip()
    return reason or None


def extract_pr_url(transcript: str) -> str:
    """Return the last PR URL in the transcript, or empty string."""
    if not transcript:
        return ""
    matches = _PR_URL_RE.findall(transcript)
    return matches[-1] if matches else ""


def detect_pr_merged(transcript: str) -> bool:
    """True if the transcript signals a confirmed merge (review/merge stage)."""
    if not transcript:
        return False
    return "merged" in transcript.lower()


# ---- ground-truth end detection (item 5) -------------------------------------

def detect_end_state(
    repo: Path,
    transcript: str,
    *,
    main_branch: str = "main",
    loop_ended: str = LOOP_NATURAL,
    expect_merge: bool = False,
    pr_url_hint: str = "",
) -> Dict[str, Any]:
    """Decide CONCLUÍDO / INCOMPLETO / BLOQUEADO from real state first.

    Priority (ground-truth first; the model's *format* is NOT trusted):

      1. **BLOQUEADO** — the agent declared ``BLOQUEADO: <motivo>``. This is the
         single agent-sourced signal (only it can know a hard impediment), and
         it wins even over a partial diff: a block means "do not auto-resume".
      2. **CONCLUÍDO** — there is a confirmed PR (URL present), and, for the
         review/merge stage, a confirmed merge. The PR existing is the real
         definition of done from the issue's perspective.
      3. **INCOMPLETO** — anything else: ran out of rounds / timed out / ended
         naturally without a PR. Resumable.

    Returns the structured result the worker embeds and the pipeline reads:
    ``{ended, pr_url, motivo_bloqueio, motivo_fim_loop}``. ``fingerprint`` and
    ``tentativa`` are added by the worker (they need the workspace state).
    """
    pr_url = extract_pr_url(transcript) or (pr_url_hint or "")
    merged = detect_pr_merged(transcript)
    blocked_reason = extract_blocked_reason(transcript)

    result: Dict[str, Any] = {
        "ended": ENDED_INCOMPLETO,
        "pr_url": pr_url,
        "motivo_bloqueio": "",
        "motivo_fim_loop": loop_ended,
    }

    if blocked_reason:
        result["ended"] = ENDED_BLOQUEADO
        result["motivo_bloqueio"] = blocked_reason
        return result

    if pr_url and (merged or not expect_merge):
        result["ended"] = ENDED_CONCLUIDO
        return result

    # No PR (or merge expected but not confirmed) → incomplete/resumable.
    result["ended"] = ENDED_INCOMPLETO
    return result
