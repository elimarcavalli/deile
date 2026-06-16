"""Resolve every directory that contributes skills, in priority order.

Scan order (lowest to highest priority — later sources override earlier):

1. **Bundled** — ``deile/skills/library/**/*.md``
2. **User** — ``~/.deile/skills/*.md``
3. **User-Claude** — ``~/.claude/commands/*.md`` (UPPERCASE names, ``kind=command``)
4. **Org** — paths from ``Settings.org_skills_paths`` (above user, below project)
5. **Project** — ``<cwd>/.deile/skills/*.md``
6. **Project-Claude** — ``<cwd>/.claude/commands/*.md`` (UPPERCASE names)
7. **Extras** — paths added via ``SettingsManager.get_all_skills_paths()``
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .base import Skill
from .loader import parse_skill_text

logger = logging.getLogger(__name__)


_BUNDLED_LIBRARY_DIR = Path(__file__).resolve().parent / "library"


@dataclass(frozen=True)
class ScanEntry:
    """One ``(directory, source, kind, force_uppercase)`` row in the scan order."""

    directory: Path
    source: str
    kind: str
    force_uppercase_name: bool


def default_scan_order(
    *,
    project_dir: Optional[Path] = None,
    user_home: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
    org_paths: Iterable[Path] = (),
) -> List[ScanEntry]:
    """Return the canonical scan order — lowest to highest priority.

    ``org_paths`` are inserted between user-claude and project, giving org
    skills higher priority than user but lower priority than project (issue #741).
    """
    project = project_dir or Path.cwd()
    home = user_home or Path.home()

    order: List[ScanEntry] = [
        ScanEntry(_BUNDLED_LIBRARY_DIR, "bundled", "skill", False),
        ScanEntry(home / ".deile" / "skills", "user", "skill", False),
        ScanEntry(home / ".claude" / "commands", "user", "command", True),
    ]
    for org_path in org_paths:
        order.append(ScanEntry(Path(org_path), "org", "skill", False))
    order += [
        ScanEntry(project / ".deile" / "skills", "project", "skill", False),
        ScanEntry(project / ".claude" / "commands", "project", "command", True),
    ]
    for extra in extra_paths:
        order.append(ScanEntry(Path(extra), "extra", "skill", False))
    return order


def discover_skills_sync(
    *,
    project_dir: Optional[Path] = None,
    user_home: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
    org_paths: Iterable[Path] = (),
) -> Tuple[List[Skill], List[Tuple[str, Path, Path]]]:
    """Sync discovery. Returns ``(skills, overrides)``."""
    merged: dict = {}
    overrides: List[Tuple[str, Path, Path]] = []

    for entry in default_scan_order(
        project_dir=project_dir, user_home=user_home, extra_paths=extra_paths,
        org_paths=org_paths,
    ):
        if not entry.directory.is_dir():
            continue
        for md_path in sorted(entry.directory.rglob("*.md")):
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "skills: cannot read skill file %s (%s: %s); skipped",
                    md_path, type(exc).__name__, exc,
                )
                continue
            except UnicodeDecodeError as exc:
                logger.warning(
                    "skills: skill file %s is not valid UTF-8 (%s); skipped",
                    md_path, exc,
                )
                continue
            skill = parse_skill_text(
                text, md_path,
                source=entry.source,
                kind=entry.kind,
                force_uppercase_name=entry.force_uppercase_name,
            )
            if skill is None:
                continue
            if skill.name in merged:
                overrides.append((skill.name, merged[skill.name].source_path, skill.source_path))
            merged[skill.name] = skill

    return list(merged.values()), overrides


async def discover_skills(
    *,
    project_dir: Optional[Path] = None,
    user_home: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
    org_paths: Iterable[Path] = (),
) -> Tuple[List[Skill], List[Tuple[str, Path, Path]]]:
    """Async wrapper — runs ``discover_skills_sync`` off-thread."""
    return await asyncio.to_thread(
        discover_skills_sync,
        project_dir=project_dir,
        user_home=user_home,
        extra_paths=extra_paths,
        org_paths=org_paths,
    )
