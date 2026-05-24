"""SkillRegistry — keeps loaded skills and exposes them by name.

Thread-safety: every mutation and every read of the internal dict is
guarded by an :class:`threading.RLock`. The hot-reload watcher mutates the
registry from its own thread; readers (the per-turn router and the
``invoke_skill`` tool) can be called from the main asyncio loop. The lock
covers both single-op mutations and compound operations like
``replace_all`` so a reader never sees a torn intermediate state.

The singleton accessor itself is also thread-safe — the constructor lock
prevents two threads from each instantiating their own copy.
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
        """Add *skill* to the registry. A duplicate name replaces the previous entry.

        Duplicates are logged at INFO so a deliberate user override (e.g. a project
        ``python.md`` shadowing the bundled one) is visible without being noisy.
        """
        with self._lock:
            previous = self._skills.get(skill.name)
            self._skills[skill.name] = skill
        if previous is not None:
            logger.info(
                "Skill '%s' replaced: %s → %s",
                skill.name,
                previous.source_path,
                skill.source_path,
            )

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._skills.pop(name, None) is not None

    def get(self, name: str) -> Optional[Skill]:
        with self._lock:
            return self._skills.get(name)

    def list_all(self) -> List[Skill]:
        # Snapshot under lock — callers iterate the returned list freely.
        with self._lock:
            return list(self._skills.values())

    def list_names(self) -> List[str]:
        with self._lock:
            return sorted(self._skills.keys())

    def clear(self) -> None:
        with self._lock:
            self._skills.clear()

    def replace_all(self, skills: Iterable[Skill]) -> None:
        """Atomically swap the registry contents to *skills*.

        Use this from the hot-reload path so concurrent readers never observe
        a torn state where some skills have been removed but new ones not
        yet added. Equivalent to ``clear()`` followed by ``register()`` for
        each skill, but performed under a single lock.
        """
        new_state: Dict[str, Skill] = {}
        for skill in skills:
            new_state[skill.name] = skill
        with self._lock:
            self._skills = new_state

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
    """Return the process-wide ``SkillRegistry`` singleton (lazily created).

    Thread-safe: two threads racing to first-access will agree on a single
    instance (the lock excludes both from running the constructor twice).
    """
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
