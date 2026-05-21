"""Numeric helpers — pure integer predicates."""

from __future__ import annotations


def is_impar(n: int) -> bool:
    """Return True if *n* is odd (ímpar in Portuguese).

    Works for any integer: positive, negative, or zero.
    Zero is even, so ``is_impar(0) → False``.
    """
    return n % 2 != 0
