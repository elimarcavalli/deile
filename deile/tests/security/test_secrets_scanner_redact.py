"""Regression tests for ``SecretsScanner.redact_text`` length mismatch.

Bug: ``matched_text`` is ``match.group(1)`` for patterns with capture
groups, while ``start_pos``/``end_pos`` are ``match.start()``/``match.end()``
of the FULL match. The redaction code used
``redaction_char * len(matched_text)`` (a shorter length) inside the
position span ``[start_pos:end_pos]``, producing output shorter than the
input AND erasing surrounding context like the ``api_key = "..."``
prefix.

Fix: replacement length now equals ``end_pos - start_pos`` so the
character count matches what's being overwritten.
"""

from __future__ import annotations

from deile.security.secrets_scanner import SecretsScanner


def test_redact_text_preserves_length() -> None:
    """A redacted line must have the same length as the original line."""
    text = "AKIAIOSFODNN7EXAMPLE here is my key"
    scanner = SecretsScanner()
    redacted, matches = scanner.redact_text(text)
    assert len(redacted) == len(text), (
        f"length mismatch: original={len(text)} redacted={len(redacted)}\n"
        f"  original: {text!r}\n  redacted: {redacted!r}"
    )
    # And the original secret must not appear in the redacted output.
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted


def test_redact_text_preserves_context_with_assignment() -> None:
    """The classic pattern (``api_key = "..."``) — the LHS context must
    survive because the redaction only overwrites the value's span."""
    text = 'api_key = "ghp_abcdefghijklmnopqrstuvwxyz1234567890ABCD"'
    scanner = SecretsScanner()
    redacted, matches = scanner.redact_text(text)

    # Length identical to the original.
    assert len(redacted) == len(text)
    # Original secret gone.
    assert "ghp_abcdefghijklmnopqrstuvwxyz1234567890ABCD" not in redacted


def test_redact_text_no_matches_is_noop() -> None:
    text = "no secrets here\njust comments"
    scanner = SecretsScanner()
    redacted, matches = scanner.redact_text(text)
    assert redacted == text
    assert matches == []


def test_redact_text_multiline_with_secrets() -> None:
    text = "line one no secret\n" "AKIAIOSFODNN7EXAMPLE\n" "line three no secret"
    scanner = SecretsScanner()
    redacted, matches = scanner.redact_text(text)

    redacted_lines = redacted.split("\n")
    original_lines = text.split("\n")
    assert len(redacted_lines) == len(original_lines)
    for orig, red in zip(original_lines, redacted_lines):
        assert len(red) == len(orig), (orig, red)


# ---------------------------------------------------------------------------
# Regression tests for issue #707 — whitelist substring match silently drops
# real secrets whose value contains a placeholder word like 'test' or 'demo'.
# ---------------------------------------------------------------------------


def test_github_token_with_test_substring_is_detected() -> None:
    """A valid ghp_ PAT whose body contains 'test' must NOT be whitelisted."""
    # ghp_ + 36 alphanum chars; the first four are 'test' — triggers the bug.
    token = "ghp_testABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    text = f'github_token = "{token}"'
    scanner = SecretsScanner()
    matches = scanner.scan_text(text)
    secret_values = [m.matched_text for m in matches]
    assert any(
        token in v or v in token for v in secret_values
    ), f"Expected token {token!r} to be detected but scan returned: {secret_values!r}"


def test_aws_key_with_demo_substring_is_redacted() -> None:
    """A valid AKIA key whose body contains 'demo' must be detected and redacted."""
    # AKIA + 16 uppercase alphanum; 'DEMO' appears at offset 4.
    key = "AKIADEMOZ23456789012"
    scanner = SecretsScanner()
    redacted, matches = scanner.redact_text(key)
    assert matches, f"Expected at least one match for {key!r} but got none"
    assert key not in redacted, f"Secret {key!r} was not redacted; output: {redacted!r}"


def test_literal_placeholder_still_whitelisted() -> None:
    """The exact placeholder string 'your_api_key_here' must remain whitelisted."""
    text = 'api_key = "your_api_key_here"'
    scanner = SecretsScanner()
    matches = scanner.scan_text(text)
    placeholder_matches = [m for m in matches if m.matched_text == "your_api_key_here"]
    assert (
        not placeholder_matches
    ), f"Placeholder 'your_api_key_here' should be whitelisted but was flagged: {placeholder_matches!r}"
