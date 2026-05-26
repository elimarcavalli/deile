"""Guard against re-introducing hardcoded ``github.com`` references in code.

Allowed loci (the union of grep --exclude rules):

* The forge layer itself (it IS GitHub-aware by design).
* The legacy shim ``pipeline/github_client.py`` (re-exports for compat).
* Documentation and tests.
* Comments mentioning the legacy regex / shape (we tolerate prose).

The check looks for the literal string ``"github.com"`` in production code
outside the allowlist. Each new match here means a forge-agnostic
abstraction was bypassed — open the file and replace the URL with a
``ForgeConfig`` helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Repo root resolved from this test file's location: tests/orchestration/forge/x.py
# Go up 4 levels to reach the repo root.
REPO_ROOT = Path(__file__).resolve().parents[4]


def _is_allowed(path: Path) -> bool:
    parts = path.relative_to(REPO_ROOT).parts
    rel_name = str(path.relative_to(REPO_ROOT))
    # Allow tests, docs, forge layer, the deprecation shim and a small
    # explicit list of files that legitimately mention ``github.com``:
    #
    # - ``settings.py`` — declares ``forge_github_host: str = "github.com"``
    #   (a default value, not GH-specific logic).
    # - ``_shared.py``  — DEILE-project metadata table (the URL OF THIS REPO).
    # - ``briefs.py`` / ``claude_dispatcher.py`` — the ``_default_forge_config``
    #   helpers that fall back to a GH config when no forge is passed
    #   (preserves byte-exact GH behaviour for legacy test callers).
    # - ``worktree_manager.py`` — ``_FORGE_HOST_HINTS`` lists known forge
    #   hostname fragments and the ``"github.com" in url`` check that
    #   keeps the legacy ``github`` remote alias alive on GH repos.
    legacy_known_files = {
        "deile/config/settings.py",
        "deile/commands/builtin/_shared.py",
        "deile/orchestration/pipeline/briefs.py",
        "deile/orchestration/pipeline/claude_dispatcher.py",
        "deile/orchestration/pipeline/worktree_manager.py",
        # The standup collectors documents the URL formats it parses for
        # the ``/standup`` slash command. Mentions are docstring/comments,
        # not coupling — parsing is done by ``parse_forge_url`` internally.
        "deile/commands/builtin/_standup_collectors.py",
    }
    return (
        parts[0] in {".git", ".github", "docs", "deilebot", "test-your-might"}
        or "tests" in parts
        or "forge" in parts
        or path.name == "github_client.py"
        or rel_name in legacy_known_files
        or path.suffix in {".md", ".yaml", ".yml", ".json"}
    )


def test_no_hardcoded_github_com_in_production_python():
    """Scan ``deile/`` for stray ``github.com`` literals in production code."""
    offenders: list[str] = []
    for path in (REPO_ROOT / "deile").rglob("*.py"):
        if _is_allowed(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "github.com" in line and "# noqa: forge" not in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "hardcoded github.com references in production code — replace with "
        "ForgeConfig helpers (web_pr_url / web_issue_url) or move to forge/. "
        "Offenders:\n  " + "\n  ".join(offenders)
    )


def test_no_bare_gh_cli_in_pipeline_briefs():
    """No literal ``gh pr create``/``gh issue view`` in the brief templates —
    they must use the ``{forge_*_cmd}`` placeholders so they render to
    ``glab`` for a GitLab project."""
    briefs = REPO_ROOT / "deile" / "orchestration" / "pipeline" / "briefs.py"
    text = briefs.read_text(encoding="utf-8")
    # Look only at the LITERAL TEMPLATE STRINGS (lines containing the
    # placeholders). The render helpers themselves do mention "gh" /
    # "glab" in fallbacks, which is allowed.
    forbidden = [
        "gh pr create", "gh pr view", "gh issue view --comments",
        "gh issue edit", "gh issue comment", "gh issue create --repo",
    ]
    inside_template_block = False
    offenders = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.startswith('_WORKER_') and '"""' in line:
            inside_template_block = True
            continue
        if inside_template_block and line.strip().endswith('"""'):
            inside_template_block = False
            continue
        if inside_template_block:
            for token in forbidden:
                if token in line:
                    offenders.append(f"briefs.py:{lineno}: {line.strip()}")
    assert not offenders, (
        "Forbidden literal CLI commands inside brief templates — use "
        "{forge_*_cmd} placeholders. Offenders:\n  " + "\n  ".join(offenders)
    )
