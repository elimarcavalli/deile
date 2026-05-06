"""Tests for deile.common.text_utils."""

import pytest

from deile.common.text_utils import slug


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Olá Mundo!", "ola-mundo"),
        ("  espaços  ", "espacos"),
        ("a/b/c", "a-b-c"),
        ("---", ""),
        ("Açaí 100%!!", "acai-100"),
    ],
)
def test_slug(raw, expected):
    assert slug(raw) == expected
