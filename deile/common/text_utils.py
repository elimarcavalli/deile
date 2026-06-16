"""Text manipulation helpers."""

from __future__ import annotations

import re
import unicodedata


def slug(text: str) -> str:
    """Convert text to a URL-friendly slug.

    Lowercases, strips accents, replaces non-alphanumeric runs with a single
    hyphen, and trims leading/trailing hyphens.
    """
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    lowered = ascii_only.lower()
    hyphenated = re.sub(r"[^a-z0-9]+", "-", lowered)
    return hyphenated.strip("-")


def truncate(
    text, limit: int, *, flatten_newlines: bool = False, ellipsis: str = "…"
) -> str:
    """Trunca ``text`` para no máximo ``limit`` caracteres com elipse.

    Args:
        text: valor a truncar (None → "").
        limit: tamanho máximo do resultado incluindo a elipse.
        flatten_newlines: se True, substitui ``\\n`` por ``" ⏎ "`` e ``\\r`` por
            ``" "`` antes de truncar (modo "uma linha"). Default ``False``.
        ellipsis: caractere de truncamento (default ``"…"``).

    Returns:
        String com no máximo ``limit`` caracteres. Se truncada, termina em
        ``ellipsis``.
    """
    if text is None:
        return ""
    s = str(text)
    if flatten_newlines:
        s = s.replace("\n", " ⏎ ").replace("\r", " ").strip()
    if len(s) <= limit:
        return s
    return s[: max(0, limit - len(ellipsis))] + ellipsis
