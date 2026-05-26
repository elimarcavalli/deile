"""Unified skills subsystem.

A skill is a Markdown file with optional YAML frontmatter that can be
auto-injected into the system prompt when triggers fire, or invoked
manually via ``/<name>``. Both modes share the same registry — see
``bootstrap.py`` for the single entry point.
"""

from .base import Skill, SkillTrigger
from .bootstrap import (BootstrapResult, bootstrap_skills,
                        bootstrap_skills_with_handle)
from .config import SkillsConfig, load_skills_config
from .discovery import discover_skills
from .language_detector import LanguageDetector
from .loader import (SkillLoader, SkillLoadError, normalize_name,
                     parse_skill_text)
from .registry import SkillRegistry, get_skill_registry, reset_skill_registry
from .router import SkillRouter, SkillSelectionContext
from .slash_command_bridge import (register_skills_as_commands,
                                   unregister_skill_commands)
from .watcher import SkillsWatcher, reload_registry

__all__ = [
    "BootstrapResult",
    "Skill",
    "SkillTrigger",
    "SkillLoader",
    "SkillLoadError",
    "SkillRegistry",
    "SkillRouter",
    "SkillSelectionContext",
    "SkillsConfig",
    "SkillsWatcher",
    "LanguageDetector",
    "bootstrap_skills",
    "bootstrap_skills_with_handle",
    "discover_skills",
    "get_skill_registry",
    "reset_skill_registry",
    "load_skills_config",
    "normalize_name",
    "parse_skill_text",
    "register_skills_as_commands",
    "unregister_skill_commands",
    "reload_registry",
]
