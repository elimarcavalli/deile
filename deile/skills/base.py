"""Skill / SkillTrigger value objects.

The ``Skill`` type is shared across two activation modes:

- **Auto-injection** (new): when ``triggers`` fire for the current turn,
  ``SkillRouter`` appends ``content`` to the system prompt.
- **Slash command** (legacy): every loaded skill is also registered as a
  ``/<name>`` slash command via ``deile.commands.skill_loader``, which runs
  ``content`` as a one-shot LLM prompt.

A skill can support both modes simultaneously — the same MD file appears in
the catalog, fires its triggers, and is invokable via ``/<name>``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class SkillTrigger:
    """Conditions under which a skill is auto-selected for the system prompt."""

    file_globs: List[str] = field(default_factory=list)
    code_block_langs: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    file_content_patterns: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        """True when no auto-injection rule is defined (slash-only skill)."""
        return not (
            self.file_globs
            or self.code_block_langs
            or self.keywords
            or self.file_content_patterns
        )


@dataclass
class Skill:
    """A discrete, composable unit of expertise loaded from disk."""

    name: str
    description: str
    body: str
    triggers: SkillTrigger = field(default_factory=SkillTrigger)
    priority: int = 0
    source: str = "bundled"  # "bundled" | "user" | "project" | "extra"
    kind: str = "skill"  # "skill" | "command" (legacy uppercase commands)
    source_path: Optional[Path] = None

    @property
    def content(self) -> str:
        """Alias for ``body``. Kept for callers that prefer the newer name."""
        return self.body
