"""Wire the unified skills subsystem at startup.

``bootstrap_skills`` is the single entry point. It:

1. Resolves every scan path (bundled + user + project + claude/commands + extras).
2. Loads every ``*.md`` skill and merges them (later sources override earlier).
3. Populates the singleton ``SkillRegistry``.
4. Optionally bridges to a command registry — every skill is also registered
   as a ``/<name>`` slash command for explicit invocation.
5. Returns a ``SkillRouter`` ready to be queried per turn.

Callers that only want the catalog (e.g. for prompt enrichment) get the
populated registry via ``get_skill_registry()``.
"""

from __future__ import annotations

import logging
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


async def bootstrap_skills(
    config: Optional[SkillsConfig] = None,
    *,
    command_registry: Any = None,
    project_dir: Optional[Path] = None,
    user_home: Optional[Path] = None,
    extra_paths: Iterable[Path] = (),
    hot_reload: bool = False,
) -> Optional[SkillRouter]:
    """Discover skills, populate the registry, and return a ready ``SkillRouter``.

    Returns ``None`` when ``config.enabled`` is False — callers should treat
    ``None`` as "skill injection off". When *command_registry* is provided,
    every loaded skill is also registered as a ``/<name>`` slash command.

    When *hot_reload* is True a :class:`SkillsWatcher` is started so
    subsequent ``.md`` edits/creates/deletes refresh the registry without an
    agent restart. The watcher reference is attached to the returned router
    as ``router._watcher`` so callers that need to ``stop()`` it on shutdown
    can reach it without us cluttering the public API.
    """
    cfg = config or load_skills_config()
    if not cfg.enabled:
        logger.debug("skills: subsystem disabled in config; skipping bootstrap")
        return None

    # ``cfg.library_paths`` (from skills.yaml) is merged with caller-supplied
    # extras — both are scanned as ``extra``-source paths on top of the
    # canonical bundled/user/project locations.
    merged_extras: list = list(cfg.library_paths) + list(extra_paths)

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
        logger.debug("skills: %r from %s overrides previous definition from %s", name, new_path, old_path)

    logger.info("skills: registry now holds %d skill(s)", len(registry))

    if command_registry is not None:
        # Wipe stale skill commands (re-bootstrap path) before registering fresh ones.
        unregister_skill_commands(command_registry)
        registered = register_skills_as_commands(skills, command_registry)
        logger.info("skills: registered %d skill(s) as /<name> slash commands", registered)

    detector = LanguageDetector(
        extension_map=cfg.extension_map,
        basename_map=cfg.basename_map,
    )
    router = SkillRouter(
        registry=registry,
        language_detector=detector,
        max_skills_per_turn=cfg.max_per_turn,
        project_root=project_dir,
    )

    if hot_reload:
        watcher = SkillsWatcher(
            project_dir=project_dir,
            user_home=user_home,
            extra_paths=merged_extras,
            command_registry=command_registry,
        )
        watcher.start()
        router._watcher = watcher  # type: ignore[attr-defined]

    return router
