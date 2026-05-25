"""SkillRegistry — keeps loaded skills and exposes them by name.

Thread-safety: every mutation and read is guarded by ``RLock``. The hot-reload
watcher mutates from its own thread; readers (the per-turn router, the
``invoke_skill`` tool) may be called from the main asyncio loop. The lock
covers compound operations like ``replace_all`` so a reader never sees a torn
state. The singleton accessor uses double-checked locking to prevent two
threads from instantiating their own copy.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .base import Skill
from .loader import SkillLoader

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Registry of all loaded skills, keyed by ``Skill.name``."""

    def __init__(self) -> None:
        self._skills: Dict[str, Skill] = {}
        self._lock = threading.RLock()

    def register(self, skill: Skill) -> None:
        with self._lock:
            previous = self._skills.get(skill.name)
            self._skills[skill.name] = skill
        if previous is not None:
            logger.info(
                "Skill '%s' replaced: %s → %s",
                skill.name, previous.source_path, skill.source_path,
            )

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._skills.pop(name, None) is not None

    def get(self, name: str) -> Optional[Skill]:
        with self._lock:
            return self._skills.get(name)

    def list_all(self) -> List[Skill]:
        with self._lock:
            return list(self._skills.values())

    def list_names(self) -> List[str]:
        with self._lock:
            return sorted(self._skills.keys())

    def clear(self) -> None:
        with self._lock:
            self._skills.clear()

    def replace_all(self, skills: Iterable[Skill]) -> None:
        """Atomically swap registry contents — readers never observe a torn state."""
        new_state: Dict[str, Skill] = {}
        for skill in skills:
            new_state[skill.name] = skill
        with self._lock:
            self._skills = new_state

    async def load_from_directories(self, directories: Iterable[Path]) -> int:
        loader = SkillLoader()
        count = 0
        for directory in directories:
            skills = await loader.load_directory(Path(directory))
            for skill in skills:
                self.register(skill)
                count += 1
        return count

    def __len__(self) -> int:
        with self._lock:
            return len(self._skills)

    def __contains__(self, name: str) -> bool:
        with self._lock:
            return name in self._skills

    def __iter__(self):
        # Iterate over a snapshot so concurrent mutations don't raise
        # RuntimeError mid-iteration.
        return iter(self.list_all())


_registry: Optional[SkillRegistry] = None
_registry_lock = threading.Lock()


def get_skill_registry() -> SkillRegistry:
    """Return the process-wide ``SkillRegistry`` singleton (lazily created, thread-safe)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = SkillRegistry()
    return _registry


def reset_skill_registry() -> None:
    """Test hook — drop the singleton so the next ``get_skill_registry`` starts fresh."""
    global _registry
    with _registry_lock:
        _registry = None
