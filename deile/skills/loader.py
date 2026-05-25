"""Unified ``Skill`` file parser.

Frontmatter format (all fields optional unless noted)::

    ---
    name: my-skill                   # default: filename stem (normalized)
    description: One-line summary    # default: ``Skill: <name>``
    triggers:                        # optional — when present, auto-injection fires
      file_globs: ["*.py"]
      code_block_langs: [python]
    priority: 50                     # default 0; higher = ranked first
    ---
    Body — required.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, List, Optional

import yaml

from .base import Skill, SkillTrigger

logger = logging.getLogger(__name__)

# Accept both Unix (LF) and Windows (CRLF) line endings — files saved by a
# Windows editor (or git core.autocrlf=true) would otherwise silently lose
# frontmatter parsing.
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", re.DOTALL)
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,63}$")


class SkillLoadError(ValueError):
    """Raised when a skill file cannot be parsed or fails schema validation."""


def normalize_name(raw: str) -> str:
    name = raw.strip().lower()
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"[^a-z0-9\-]", "", name)
    return name.strip("-")


def parse_skill_text(
    text: str,
    source_path: Path,
    *,
    source: str = "bundled",
    kind: str = "skill",
    force_uppercase_name: bool = False,
) -> Optional[Skill]:
    """Parse skill MD text into a ``Skill`` or return None when unrecoverable.

    Returns None (with a logged warning) for recoverable problems — invalid
    YAML, invalid name after normalization, empty body — to match the legacy
    lenient behavior so one bad file does not abort a whole directory scan.
    """
    frontmatter: dict = {}
    body = text

    match = _FRONTMATTER_RE.match(text)
    if match:
        try:
            parsed = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            logger.warning(
                "Skill file %s has invalid YAML front-matter and will be skipped: %s",
                source_path, exc,
            )
            return None
        if isinstance(parsed, dict):
            frontmatter = parsed
        body = text[match.end():]

    raw_name = frontmatter.get("name") or normalize_name(source_path.stem)
    if not isinstance(raw_name, str):
        logger.warning(
            "Skill file %s: 'name' must be a string (got %s) — using filename stem",
            source_path, type(raw_name).__name__,
        )
        raw_name = normalize_name(source_path.stem)

    name = normalize_name(raw_name)
    if force_uppercase_name:
        name = name.upper()

    if not _VALID_NAME_RE.match(name):
        logger.warning("Skill file %s produces invalid name %r — skipped", source_path, name)
        return None

    raw_desc = frontmatter.get("description")
    if raw_desc is not None and not isinstance(raw_desc, str):
        logger.warning(
            "Skill file %s: 'description' must be a string (got %s) — using default",
            source_path, type(raw_desc).__name__,
        )
        raw_desc = None
    description = (raw_desc or "").strip() or f"Skill: {name}"

    body = body.strip()
    if not body:
        logger.warning("Skill file %s has an empty body — skipped", source_path)
        return None

    # Reject bools explicitly — ``isinstance(True, int)`` is True in Python so
    # ``int(True)`` returns 1, silently coercing YAML ``priority: yes`` to 1.
    priority_raw = frontmatter.get("priority", 0)
    if isinstance(priority_raw, bool) or not isinstance(priority_raw, (int, float, str)):
        logger.warning(
            "Skill file %s: 'priority' must be an integer (got %s: %r) — defaulting to 0",
            source_path, type(priority_raw).__name__, priority_raw,
        )
        priority = 0
    else:
        try:
            priority = int(priority_raw)
        except (TypeError, ValueError):
            logger.warning(
                "Skill file %s: 'priority' not parseable as int (%r) — defaulting to 0",
                source_path, priority_raw,
            )
            priority = 0

    return Skill(
        name=name,
        description=description,
        body=body,
        triggers=_build_trigger(frontmatter.get("triggers"), source_path),
        priority=priority,
        source=source,
        kind=kind,
        source_path=source_path,
    )


def _build_trigger(raw: Any, source_path: Path) -> SkillTrigger:
    if raw is None:
        return SkillTrigger()
    if not isinstance(raw, dict):
        logger.warning("Skill file %s: 'triggers' must be a mapping — ignoring", source_path)
        return SkillTrigger()

    def _str_list(value: Any, field_name: str) -> List[str]:
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            logger.warning(
                "Skill file %s: 'triggers.%s' must be a list of strings — ignoring",
                source_path, field_name,
            )
            return []
        return [v.strip() for v in value if v and v.strip()]

    return SkillTrigger(
        file_globs=_str_list(raw.get("file_globs"), "file_globs"),
        code_block_langs=[lang.lower() for lang in _str_list(raw.get("code_block_langs"), "code_block_langs")],
        keywords=_str_list(raw.get("keywords"), "keywords"),
        file_content_patterns=_str_list(raw.get("file_content_patterns"), "file_content_patterns"),
    )


class SkillLoader:
    """Loads ``Skill`` objects from individual files or directories."""

    async def load_file(self, path: Path, **kwargs: Any) -> Skill:
        """Load *path* into a ``Skill``. Raises ``SkillLoadError`` for hard failures.

        Unlike directory scans (which warn and skip), this raises so test code
        can assert specific failure modes.
        """
        text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        skill = parse_skill_text(text, path, **kwargs)
        if skill is None:
            raise SkillLoadError(f"{path}: could not be parsed")
        return skill

    async def load_directory(
        self,
        directory: Path,
        *,
        source: str = "bundled",
        kind: str = "skill",
        force_uppercase_name: bool = False,
    ) -> List[Skill]:
        if not directory.exists() or not directory.is_dir():
            return []

        md_files = await asyncio.to_thread(_list_md_files, directory)
        skills: List[Skill] = []
        for md_path in md_files:
            text = await asyncio.to_thread(md_path.read_text, encoding="utf-8")
            skill = parse_skill_text(
                text, md_path,
                source=source, kind=kind, force_uppercase_name=force_uppercase_name,
            )
            if skill is not None:
                skills.append(skill)
        return skills


def _list_md_files(directory: Path) -> List[Path]:
    return sorted(directory.rglob("*.md"))
