"""Unit tests for ``deile/tools/_hash_utils.sha8``.

The helper consolidates two previous ``_sha8`` copies (in messaging/_base
and vision_tool). Audit logs depend on it producing a stable, length-8
hex digest from either ``str`` or ``bytes`` payloads. Direct unit tests
are required so a regression to the encoding rule, the truncation, or
the surrogate-replace policy fails fast.
"""
from __future__ import annotations

import hashlib

import pytest

from deile.tools._hash_utils import sha8


def test_sha8_length():
    assert len(sha8("hello")) == 8


def test_sha8_str_equals_utf8_bytes():
    assert sha8("hello") == sha8(b"hello")


def test_sha8_str_uses_utf8_encoding():
    expected = hashlib.sha256("hello".encode("utf-8")).hexdigest()[:8]
    assert sha8("hello") == expected


def test_sha8_handles_lone_surrogate():
    digest = sha8("\ud800")
    assert len(digest) == 8


def test_sha8_truncates_to_8_chars():
    full = hashlib.sha256(b"abc").hexdigest()
    assert sha8(b"abc") == full[:8]
    assert len(sha8(b"abc")) == 8


def test_sha8_distinct_inputs_give_distinct_digests():
    assert sha8("foo") != sha8("bar")


@pytest.mark.parametrize("payload", [b"", "", b"x" * 10_000, "y" * 10_000])
def test_sha8_accepts_edge_lengths(payload):
    assert len(sha8(payload)) == 8
