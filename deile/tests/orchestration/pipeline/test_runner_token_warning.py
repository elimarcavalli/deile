"""Tests for the forge-token warning emitted by deile.pipeline.runner at startup.

Issue #415: pipeline should emit a WARNING when neither GITHUB_TOKEN nor
GITLAB_TOKEN is present, without interrupting execution.
"""
from __future__ import annotations

import logging

import pytest

from deile.orchestration.pipeline.runner import _warn_if_no_forge_token


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _capture_warnings(caplog, env, monkeypatch):
    """Run _warn_if_no_forge_token with the given env overrides."""
    for key in ("GITHUB_TOKEN", "GITLAB_TOKEN", "GL_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, val in env.items():
        monkeypatch.setenv(key, val)

    with caplog.at_level(logging.WARNING, logger="deile.pipeline.runner"):
        _warn_if_no_forge_token()

    return [r for r in caplog.records if r.name == "deile.pipeline.runner"]


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_warns_when_no_tokens(caplog, monkeypatch):
    """WARNING is emitted if neither GITHUB_TOKEN nor GITLAB_TOKEN is set."""
    records = _capture_warnings(caplog, {}, monkeypatch)
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.WARNING
    assert "GITHUB_TOKEN" in rec.message
    assert "GITLAB_TOKEN" in rec.message


def test_no_warning_with_github_token(caplog, monkeypatch):
    """No WARNING when GITHUB_TOKEN is present."""
    records = _capture_warnings(caplog, {"GITHUB_TOKEN": "ghp_fake"}, monkeypatch)
    assert records == []


def test_no_warning_with_gitlab_token(caplog, monkeypatch):
    """No WARNING when GITLAB_TOKEN is present."""
    records = _capture_warnings(caplog, {"GITLAB_TOKEN": "glpat-fake"}, monkeypatch)
    assert records == []


def test_no_warning_with_gl_token_alias(caplog, monkeypatch):
    """No WARNING when GL_TOKEN alias is present."""
    records = _capture_warnings(caplog, {"GL_TOKEN": "glpat-alias"}, monkeypatch)
    assert records == []


def test_no_warning_with_both_tokens(caplog, monkeypatch):
    """No WARNING when both tokens are set."""
    records = _capture_warnings(
        caplog,
        {"GITHUB_TOKEN": "ghp_fake", "GITLAB_TOKEN": "glpat-fake"},
        monkeypatch,
    )
    assert records == []


def test_warning_does_not_raise(monkeypatch):
    """_warn_if_no_forge_token must never raise — warning only, not fatal."""
    for key in ("GITHUB_TOKEN", "GITLAB_TOKEN", "GL_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    _warn_if_no_forge_token()  # must not raise
