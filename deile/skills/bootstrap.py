"""Wire the unified skills subsystem at startup.

``bootstrap_skills_with_handle`` is the canonical entry point: it discovers
every scan path, merges skills (later sources override earlier), populates
the singleton ``SkillRegistry``, optionally bridges to a command registry,
and returns a ``BootstrapResult`` containing the router and (when requested)
the watcher.

``bootstrap_skills`` is a thin legacy wrapper that returns just the router —
preserved because it's part of the public API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import SkillsConfig, load_skills_config
from .discovery import discover_skills
from .language_detector import LanguageDetector
from .registry import get_skill_registry
from .router import SkillRouter
from .slash_command_bridge import register_skills_as_commands, unregister_skill_commands
from .watcher import SkillsWatcher

logger = logging.getLogger(__name__)


@dataclass
class BootstrapResult:
    """Router + optional watcher handle. ``watcher`` is None unless ``hot_reload=True``."""

    router: SkillRouter
    watcher: Optional[SkillsWatcher] = None


async def bootstrap_skills_with_handle(
    config: Optional[SkillsConfig] = None,
    *,
    command_registry: Any = None,
    project_dir: Optional[Path] = None,
    user_home: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
    hot_reload: bool = False,
) -> Optional[BootstrapResult]:
    """Discover skills, populate the registry, return ``BootstrapResult`` (or None when disabled)."""
    cfg = config or load_skills_config()
    if not cfg.enabled:
        logger.debug("skills: subsystem disabled in config; skipping bootstrap")
        return None

    merged_extras = list(cfg.library_paths) + list(extra_paths)

    skills, overrides = await discover_skills(
        project_dir=project_dir,
        user_home=user_home,
        extra_paths=merged_extras,
    )

    # Merge (not clear+repopulate) so callers that ran SkillLoader at agent
    # init don't lose extras registered with their own SettingsManager.
    registry = get_skill_registry()
    for skill in skills:
        registry.register(skill)
    for name, old_path, new_path in overrides:
        logger.debug(
            "skills: %r from %s overrides previous definition from %s",
            name,
            new_path,
            old_path,
        )

    logger.info("skills: registry now holds %d skill(s)", len(registry))

    if command_registry is not None:
        unregister_skill_commands(command_registry)
        registered = register_skills_as_commands(skills, command_registry)
        logger.info(
            "skills: registered %d skill(s) as /<name> slash commands", registered
        )

    router = SkillRouter(
        registry=registry,
        language_detector=LanguageDetector(
            extension_map=cfg.extension_map,
            basename_map=cfg.basename_map,
        ),
        max_skills_per_turn=cfg.max_per_turn,
        project_root=project_dir,
    )

    watcher: Optional[SkillsWatcher] = None
    if hot_reload:
        watcher = SkillsWatcher(
            project_dir=project_dir,
            user_home=user_home,
            extra_paths=merged_extras,
            command_registry=command_registry,
        )
        watcher.start()
        router.watcher = watcher

    return BootstrapResult(router=router, watcher=watcher)


async def bootstrap_skills(
    config: Optional[SkillsConfig] = None,
    *,
    command_registry: Any = None,
    project_dir: Optional[Path] = None,
    user_home: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
    hot_reload: bool = False,
) -> Optional[SkillRouter]:
    """Legacy wrapper that returns just the router (or None when disabled)."""
    result = await bootstrap_skills_with_handle(
        config,
        command_registry=command_registry,
        project_dir=project_dir,
        user_home=user_home,
        extra_paths=extra_paths,
        hot_reload=hot_reload,
    )
    return result.router if result is not None else None
