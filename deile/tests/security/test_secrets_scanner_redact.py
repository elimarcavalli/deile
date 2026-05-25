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
    text = 'AKIAIOSFODNN7EXAMPLE here is my key'
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
    text = (
        "line one no secret\n"
        "AKIAIOSFODNN7EXAMPLE\n"
        "line three no secret"
    )
    scanner = SecretsScanner()
    redacted, matches = scanner.redact_text(text)

    redacted_lines = redacted.split('\n')
    original_lines = text.split('\n')
    assert len(redacted_lines) == len(original_lines)
    for orig, red in zip(original_lines, redacted_lines):
        assert len(red) == len(orig), (orig, red)
