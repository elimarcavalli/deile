"""SkillRegistry — keeps loaded skills and exposes them by name."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .base import Skill
from .loader import SkillLoader

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Registry of all loaded skills, keyed by ``Skill.name``."""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Add *skill* to the registry. A duplicate name replaces the previous entry.

        Duplicates are logged at INFO so a deliberate user override (e.g. a project
        ``python.md`` shadowing the bundled one) is visible without being noisy.
        """
        if skill.name in self._skills:
            logger.info(
                "Skill '%s' replaced: %s → %s",
                skill.name,
                self._skills[skill.name].source_path,
                skill.source_path,
            )
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> bool:
        return self._skills.pop(name, None) is not None

    def get(self, name: str) -> Optional[Skill]:
        return self._skills.get(name)

    def list_all(self) -> List[Skill]:
        return list(self._skills.values())

    def list_names(self) -> List[str]:
        return sorted(self._skills.keys())

    def clear(self) -> None:
        self._skills.clear()

    async def load_from_directories(self, directories: Iterable[Path]) -> int:
        """Discover and register skills from every directory in *directories*.

        Returns the count of skills registered (replacements count as 1).
        """
        loader = SkillLoader()
        count = 0
        for directory in directories:
            skills = await loader.load_directory(Path(directory))
            for skill in skills:
                self.register(skill)
                count += 1
        return count

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: str) -> bool:
        return name in self._skills

    def __iter__(self):
        return iter(self._skills.values())


_registry: Optional[SkillRegistry] = None


def get_skill_registry() -> SkillRegistry:
    """Return the process-wide ``SkillRegistry`` singleton (lazily created)."""
    global _registry
    if _registry is None:
        _registry = SkillRegistry()
    return _registry


def reset_skill_registry() -> None:
    """Test hook — drop the singleton so the next ``get_skill_registry`` starts fresh."""
    global _registry
    _registry = None
