"""Tests for deile.common.numbers — is_impar()."""

import pytest

from deile.common.numbers import is_impar


@pytest.mark.parametrize(
    "n, expected",
    [
        # Ímpares
        (1, True),
        (3, True),
        (5, True),
        (7, True),
        (99, True),
        (-1, True),
        (-3, True),
        (-99, True),
        # Pares
        (2, False),
        (4, False),
        (100, False),
        (-2, False),
        (-100, False),
        # Zero
        (0, False),
    ],
)
def test_is_impar(n: int, expected: bool) -> None:
    assert is_impar(n) == expected
