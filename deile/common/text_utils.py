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
