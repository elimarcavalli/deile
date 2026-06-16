"""Testes do módulo semconv_mapping (issue #456).

Critérios de Aceite cobertos:
- AC1: spans git.*/forge.* carregam ambos attr sets (DEILE-local + SemConv) com toggle ativo.
- AC2: normalização de vcs.repository.url — 4 inputs testados.
- AC3: DEILE_OTLP_SEMCONV_ENABLED=false → SemConv ausente; DEILE-local presente.
- AC4: zero leituras de os.environ em semconv_mapping.py.
"""

from __future__ import annotations

import pytest

from deile.observability.dispatch_export import (
    emit_dispatch_received,
    emit_forge_pr_open,
    emit_forge_pr_review,
    emit_git_commit,
    emit_git_push,
)
from deile.observability.semconv_mapping import _normalize_repo_url

pytestmark = pytest.mark.unit


# ── AC2: normalização de URL ──────────────────────────────────────────────


def test_normalize_ssh_github():
    assert (
        _normalize_repo_url("git@github.com:owner/repo.git")
        == "https://github.com/owner/repo"
    )


def test_normalize_https_with_git_suffix():
    assert (
        _normalize_repo_url("https://github.com/owner/repo.git")
        == "https://github.com/owner/repo"
    )


def test_normalize_https_idempotent():
    assert (
        _normalize_repo_url("https://github.com/owner/repo")
        == "https://github.com/owner/repo"
    )


def test_normalize_ssh_gitlab():
    assert (
        _normalize_repo_url("git@gitlab.com:owner/repo.git")
        == "https://gitlab.com/owner/repo"
    )


def test_normalize_empty():
    assert _normalize_repo_url("") == ""


# ── AC4: zero os.environ em semconv_mapping.py ───────────────────────────


def test_no_os_environ_reads_in_semconv_mapping():
    import os
    import subprocess

    repo_root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    mapping_path = os.path.join(
        repo_root, "deile", "observability", "semconv_mapping.py"
    )
    # grep for actual Python code usage of os.environ (not inside comments/docstrings)
    result = subprocess.run(
        ["grep", "-nP", r"^\s*(os\.environ|import os)", mapping_path],
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode != 0
    ), f"os.environ used as code in semconv_mapping.py:\n{result.stdout}"


# ── AC1: dual-emit quando toggle ativo (default) ─────────────────────────


def test_git_commit_span_has_deile_and_semconv_attrs(in_memory_exporter):
    """emit_git_commit → child span carrega deile.git.* + vcs.ref.head.revision."""
    tid = "task-semconv-commit-1"
    emit_dispatch_received(tid, session_id="s1", model="m", branch="main")
    emit_git_commit(
        tid, repo="git@github.com:owner/myrepo.git", sha="abc123", status="ok"
    )

    spans = in_memory_exporter.get_finished_spans()
    git_spans = [s for s in spans if s.name == "git.commit"]
    assert git_spans, "expected git.commit child span"
    attrs = dict(git_spans[0].attributes)

    # DEILE-local attrs present
    assert attrs.get("deile.git.sha") == "abc123"
    assert attrs.get("deile.git.repo") == "git@github.com:owner/myrepo.git"

    # SemConv attrs present
    assert attrs.get("vcs.ref.head.revision") == "abc123"
    assert attrs.get("vcs.repository.url") == "https://github.com/owner/myrepo"


def test_git_push_span_has_deile_and_semconv_attrs(in_memory_exporter):
    """emit_git_push → child span carrega deile.git.branch + vcs.ref.head.name."""
    tid = "task-semconv-push-1"
    emit_dispatch_received(tid, session_id="s1", model="m", branch="main")
    emit_git_push(
        tid, repo="https://github.com/owner/myrepo.git", branch="feat/x", status="ok"
    )

    spans = in_memory_exporter.get_finished_spans()
    git_spans = [s for s in spans if s.name == "git.push"]
    assert git_spans, "expected git.push child span"
    attrs = dict(git_spans[0].attributes)

    assert attrs.get("deile.git.branch") == "feat/x"
    assert attrs.get("vcs.ref.head.name") == "feat/x"
    assert attrs.get("vcs.repository.url") == "https://github.com/owner/myrepo"


def test_forge_pr_open_span_has_semconv_attrs(in_memory_exporter):
    """emit_forge_pr_open → child span carrega vcs.change.id + vcs.change.state."""
    tid = "task-semconv-pr-open-1"
    emit_dispatch_received(tid, session_id="s1", model="m", branch="main")
    emit_forge_pr_open(
        tid, repo="https://github.com/owner/repo.git", pr_number=42, status="open"
    )

    spans = in_memory_exporter.get_finished_spans()
    pr_spans = [s for s in spans if s.name == "forge.pr_open"]
    assert pr_spans, "expected forge.pr_open child span"
    attrs = dict(pr_spans[0].attributes)

    assert attrs.get("deile.forge.pr_number") == 42
    assert attrs.get("vcs.change.id") == "42"
    assert attrs.get("vcs.change.state") == "open"
    assert attrs.get("vcs.repository.url") == "https://github.com/owner/repo"


def test_forge_pr_review_span_has_semconv_attrs(in_memory_exporter):
    """emit_forge_pr_review → child span carrega vcs.change.id."""
    tid = "task-semconv-pr-review-1"
    emit_dispatch_received(tid, session_id="s1", model="m", branch="main")
    emit_forge_pr_review(
        tid, repo="git@gitlab.com:owner/repo.git", pr_number=7, status="approved"
    )

    spans = in_memory_exporter.get_finished_spans()
    pr_spans = [s for s in spans if s.name == "forge.pr_review"]
    assert pr_spans, "expected forge.pr_review child span"
    attrs = dict(pr_spans[0].attributes)

    assert attrs.get("deile.forge.pr_number") == 7
    assert attrs.get("vcs.change.id") == "7"
    assert attrs.get("vcs.change.state") == "approved"
    assert attrs.get("vcs.repository.url") == "https://gitlab.com/owner/repo"


# ── AC3: toggle desligado → SemConv ausente; DEILE-local presente ─────────


def test_semconv_disabled_no_vcs_attrs(in_memory_exporter, monkeypatch):
    """DEILE_OTLP_SEMCONV_ENABLED=false → vcs.* ausentes; deile.git.* presentes."""
    from deile.observability import reset_observability_config

    monkeypatch.setenv("DEILE_OTLP_SEMCONV_ENABLED", "false")
    reset_observability_config()

    tid = "task-semconv-disabled-1"
    emit_dispatch_received(tid, session_id="s1", model="m", branch="main")
    emit_git_commit(
        tid, repo="git@github.com:owner/repo.git", sha="deadbeef", status="ok"
    )

    spans = in_memory_exporter.get_finished_spans()
    git_spans = [s for s in spans if s.name == "git.commit"]
    assert git_spans, "expected git.commit child span"
    attrs = dict(git_spans[0].attributes)

    # DEILE-local attrs must be present
    assert "deile.git.sha" in attrs
    assert attrs["deile.git.sha"] == "deadbeef"

    # SemConv attrs must be absent
    vcs_keys = [k for k in attrs if k.startswith("vcs.")]
    assert not vcs_keys, f"expected no vcs.* attrs when toggle off, got: {vcs_keys}"


def test_semconv_enabled_default_true(in_memory_exporter):
    """sem DEILE_OTLP_SEMCONV_ENABLED → default true → vcs.* presentes."""
    tid = "task-semconv-default-1"
    emit_dispatch_received(tid, session_id="s1", model="m", branch="main")
    emit_git_push(tid, repo="https://github.com/owner/repo", branch="main", status="ok")

    spans = in_memory_exporter.get_finished_spans()
    git_spans = [s for s in spans if s.name == "git.push"]
    assert git_spans, "expected git.push child span"
    attrs = dict(git_spans[0].attributes)

    assert "vcs.ref.head.name" in attrs
    assert attrs["vcs.ref.head.name"] == "main"
