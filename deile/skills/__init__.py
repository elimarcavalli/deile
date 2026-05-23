"""Unified skills subsystem.

A **Skill** is a Markdown file with optional YAML frontmatter that can be:

- **Auto-injected** into the system prompt when its ``triggers`` fire for the
  current turn (handled by ``SkillRouter`` from ``ContextManager``).
- **Manually invoked** via ``/<name>`` as a slash command (handled by the
  bridge in ``slash_command_bridge.py``).

Both modes use the same registry, so a skill cataloged here is visible
through both paths. See ``bootstrap.py`` for the single entry point.
"""

from .base import Skill, SkillTrigger
from .bootstrap import bootstrap_skills
from .config import SkillsConfig, load_skills_config
from .discovery import discover_skills
from .language_detector import LanguageDetector
from .loader import SkillLoader, SkillLoadError, normalize_name, parse_skill_text
from .registry import SkillRegistry, get_skill_registry, reset_skill_registry
from .router import SkillRouter, SkillSelectionContext
from .slash_command_bridge import register_skills_as_commands, unregister_skill_commands

__all__ = [
    "Skill",
    "SkillTrigger",
    "SkillLoader",
    "SkillLoadError",
    "SkillRegistry",
    "SkillRouter",
    "SkillSelectionContext",
    "SkillsConfig",
    "LanguageDetector",
    "bootstrap_skills",
    "discover_skills",
    "get_skill_registry",
    "reset_skill_registry",
    "load_skills_config",
    "normalize_name",
    "parse_skill_text",
    "register_skills_as_commands",
    "unregister_skill_commands",
]
