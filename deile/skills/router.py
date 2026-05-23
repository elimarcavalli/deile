"""SkillRouter — picks the skills whose triggers fire for the current turn."""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from .base import Skill
from .language_detector import LanguageDetector
from .registry import SkillRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SkillSelectionContext:
    """Per-turn inputs the router evaluates triggers against."""

    user_input: str = ""
    file_references: tuple = ()


def _matches_file_globs(globs: Iterable[str], file_refs: Iterable[str]) -> bool:
    if not globs:
        return False
    glob_list = list(globs)
    for ref in file_refs:
        name = Path(ref).name
        for pattern in glob_list:
            # Try matching against both basename and full path for "**/foo" style.
            if fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(ref, pattern):
                return True
    return False


def _matches_code_block_langs(langs: Iterable[str], detected: Iterable[str]) -> bool:
    if not langs:
        return False
    detected_set = set(detected)
    return any(lang.lower() in detected_set for lang in langs)


class SkillRouter:
    """Selects active skills for a turn based on registered trigger conditions.

    The router is sync (no I/O at match time) and deterministic: ties on
    ``priority`` are broken by ``skill.name`` so the same context always
    yields the same ordering.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        language_detector: Optional[LanguageDetector] = None,
        max_skills_per_turn: int = 4,
    ) -> None:
        self._registry = registry
        self._detector = language_detector or LanguageDetector()
        self._max = max_skills_per_turn

    def select_skills(self, context: SkillSelectionContext) -> List[Skill]:
        """Return the skills whose triggers fire, capped at ``max_skills_per_turn``."""
        if not self._registry.list_all():
            return []

        # Detect once per turn — both inputs are cheap but we still avoid redoing
        # the regex/extension scan for every skill.
        code_block_langs = self._detector.langs_in_code_blocks(context.user_input)
        file_languages = self._detector.languages_for_paths(context.file_references)

        # Treat detected file-extension languages the same as code-block fences:
        # a skill whose ``code_block_langs`` includes "python" should also fire
        # for a ``.py`` file reference even if the user did not paste a fence.
        all_detected_langs = list({*code_block_langs, *file_languages})

        matched: List[Skill] = []
        for skill in self._registry.list_all():
            trig = skill.triggers
            if _matches_file_globs(trig.file_globs, context.file_references):
                matched.append(skill)
                continue
            if _matches_code_block_langs(trig.code_block_langs, all_detected_langs):
                matched.append(skill)
                continue

        matched.sort(key=lambda s: (-s.priority, s.name))
        return matched[: self._max]

    def render_block(self, skills: List[Skill]) -> str:
        """Format selected skills as a single block ready to append to the system prompt."""
        if not skills:
            return ""
        sections: List[str] = ["## Active Skills"]
        for skill in skills:
            sections.append(f"### Skill: {skill.name}\n{skill.content}")
        return "\n\n".join(sections)
