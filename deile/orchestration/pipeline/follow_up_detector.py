"""Heuristic follow-up detector for merged PR bodies and comments.

Scans Markdown text for follow-up recommendation sections and explicit
bullet-point mentions, then classifies each item as breaking or non-breaking.
No LLM call is made — detection is fully local and deterministic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Set

# Markdown section headers that signal a follow-up block.
_SECTION_RE = re.compile(
    r"^#{1,6}\s*"
    r"(?:follow[- ]?ups?|pr[óo]ximos?\s+passos?|trabalho\s+futuro|"
    r"trabalho\s+posterior|melhorias?\s+futuras?|futuras?\s+melhorias?|"
    r"future\s+work|next\s+steps?|a\s+fazer|to[- ]?do(?:\s+list)?)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Any header at any depth — used to detect when a section ends.
_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)

# Bullet or numbered list item, optionally with a GFM checkbox.
_BULLET_RE = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s+(?:\[[ xX?]\]\s+)?(.+)",
)

# Explicit follow-up action keywords outside section headers.
_EXPLICIT_RE = re.compile(
    r"\b(?:abrir?\s+(?:uma?\s+)?issue|criar?\s+(?:uma?\s+)?(?:tarefa|issue)|"
    r"fazer?\s+em\s+outra\s+[Pp][Rr]|open\s+(?:an?\s+)?issue|"
    r"create\s+(?:a\s+)?(?:task|issue))\b",
    re.IGNORECASE,
)

# Keywords that classify an item as a breaking change (safe-by-default gate).
_BREAKING_RE = re.compile(
    r"\b(?:breaking(?:\s+change)?|mudança\s+breaking|quebra\s+(?:de\s+)?compatibilidade|"
    r"incompatível|incompatible|breaking\s+api|api\s+break|removes?\s+support)\b",
    re.IGNORECASE,
)

_MAX_ITEMS = 5
_MAX_TITLE_CHARS = 120


@dataclass(frozen=True)
class FollowUp:
    title: str
    is_breaking: bool


def detect_follow_ups(pr_body: str, pr_comments: List[str]) -> List[FollowUp]:
    """Return follow-up items found in *pr_body* and *pr_comments*.

    Items from the PR body are scanned first. Comments are scanned in order
    until *_MAX_ITEMS* is reached. Duplicates (case-insensitive title match)
    are silently dropped.
    """
    results: List[FollowUp] = []
    seen: Set[str] = set()

    for text in [pr_body, *pr_comments]:
        if not text:
            continue
        _extract_from_text(text, results, seen)
        if len(results) >= _MAX_ITEMS:
            break

    return results[:_MAX_ITEMS]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_from_text(text: str, results: List[FollowUp], seen: Set[str]) -> None:
    """Scan *text* in two passes:

    Pass 1 — section-based: collect bullet items inside follow-up sections.
    Pass 2 — global: collect any bullet with an explicit "abrir issue" phrase.
    """
    _scan_sections(text, results, seen)
    if len(results) < _MAX_ITEMS:
        _scan_explicit_bullets(text, results, seen)


def _scan_sections(text: str, results: List[FollowUp], seen: Set[str]) -> None:
    lines = text.splitlines()
    in_section = False
    section_depth = 0

    for line in lines:
        header_m = _HEADER_RE.match(line)
        if header_m:
            depth = len(line) - len(line.lstrip("#"))
            if _SECTION_RE.match(line):
                in_section = True
                section_depth = depth
            elif in_section and depth <= section_depth:
                in_section = False
            continue

        if not in_section:
            continue

        bullet_m = _BULLET_RE.match(line)
        if bullet_m:
            _add_item(bullet_m.group(1).strip(), results, seen)
            if len(results) >= _MAX_ITEMS:
                return


def _scan_explicit_bullets(text: str, results: List[FollowUp], seen: Set[str]) -> None:
    for line in text.splitlines():
        bullet_m = _BULLET_RE.match(line)
        if bullet_m and _EXPLICIT_RE.search(bullet_m.group(1)):
            _add_item(bullet_m.group(1).strip(), results, seen)
            if len(results) >= _MAX_ITEMS:
                return


def _add_item(text: str, results: List[FollowUp], seen: Set[str]) -> None:
    title = text[:_MAX_TITLE_CHARS].strip()
    if not title:
        return
    key = title.lower()
    if key in seen:
        return
    seen.add(key)
    is_breaking = bool(_BREAKING_RE.search(title))
    results.append(FollowUp(title=title, is_breaking=is_breaking))
