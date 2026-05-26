"""Tests for the GitLab token patterns added by issue #297."""

from __future__ import annotations

import pytest

from deile.security.secrets_scanner import SecretsScanner, SecretType


@pytest.fixture
def scanner():
    return SecretsScanner()


@pytest.mark.parametrize("token", [
    "glpat-abcdefghijklmnopqrstuvwxyzABC123",   # personal access token
    "gldt-abcdefghijklmnopqrstuvwxyz1234567890",  # deploy token
    "glptt-abcdefghijklmnopqrstuvwxyz12345678",   # project trigger
    "glsoat-abcdefghijklmnopqrstuvwxyz12345",     # agent OAuth
])
def test_secrets_scanner_gitlab_prefix_tokens_flagged(scanner, token):
    matches = scanner.scan_text(f"my_token={token}")
    assert any(
        m.secret_type is SecretType.GITLAB_TOKEN for m in matches
    ), f"{token!r} not flagged as GITLAB_TOKEN"


def test_secrets_scanner_flags_named_env(scanner):
    matches = scanner.scan_text('GITLAB_TOKEN="abcdefghijklmnopqrstuvwxyz12345"')
    assert any(m.secret_type is SecretType.GITLAB_TOKEN for m in matches)


def test_secrets_scanner_flags_gl_token_alias(scanner):
    matches = scanner.scan_text("GL_TOKEN=abcdefghijklmnopqrstuvwxyz12345")
    assert any(m.secret_type is SecretType.GITLAB_TOKEN for m in matches)


def test_secrets_scanner_flags_ci_job_token(scanner):
    matches = scanner.scan_text('CI_JOB_TOKEN="abcdefghijklmnopqrstuvwxyz12345"')
    assert any(m.secret_type is SecretType.GITLAB_TOKEN for m in matches)


def test_secrets_scanner_does_not_misclassify_github_pat(scanner):
    """A GitHub PAT must be classified as ``GITHUB_TOKEN`` and NOT as
    ``GITLAB_TOKEN`` — the patterns must be disjoint."""
    matches = scanner.scan_text("ghp_abcdefghijklmnopqrstuvwxyz123456789012")
    gh = [m for m in matches if m.secret_type is SecretType.GITHUB_TOKEN]
    gl = [m for m in matches if m.secret_type is SecretType.GITLAB_TOKEN]
    assert gh and not gl, "GitHub PAT must not match the GitLab patterns"


def test_secrets_scanner_does_not_flag_unrelated_strings(scanner):
    matches = scanner.scan_text("hello world; gl-something-short")
    assert not any(m.secret_type is SecretType.GITLAB_TOKEN for m in matches)
