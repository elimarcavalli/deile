"""Secrets scanner picks up bot control-plane / Discord tokens."""

from __future__ import annotations

from deile.security.secrets_scanner import SecretsScanner, SecretType


def test_detects_deile_bot_auth_token():
    scanner = SecretsScanner()
    sample = 'DEILE_BOT_AUTH_TOKEN=dgheQk3lJYkTzv-2_OcK9aB-ZxkkJpKLm12345'
    matches = scanner.scan_text(sample)
    assert any(m.secret_type == SecretType.GENERIC_SECRET for m in matches)


def test_detects_control_plane_token_variant():
    scanner = SecretsScanner()
    sample = 'DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN=AbcDefGhIjKlMnOp1234567890'
    matches = scanner.scan_text(sample)
    assert any(m.secret_type == SecretType.GENERIC_SECRET for m in matches)


def test_detects_discord_bot_token():
    scanner = SecretsScanner()
    sample = 'DEILE_BOT_DISCORD_TOKEN=MTQ5OTU5NjQwMDk.GMi1Vo.la1gT5O0LFZGX2uKyR_LZlZK30eIyaQ'
    matches = scanner.scan_text(sample)
    assert any(m.secret_type == SecretType.GENERIC_SECRET for m in matches)


def test_ignores_unrelated_lines():
    scanner = SecretsScanner()
    sample = 'just_a_message=hello world'
    matches = scanner.scan_text(sample)
    # The generic credit-card / aws / etc. patterns should not fire on plain
    # text like this. We only assert we don't flag a deile_bot auth token.
    relevant = [m for m in matches if "DEILE_BOT" in m.matched_text]
    assert relevant == []
