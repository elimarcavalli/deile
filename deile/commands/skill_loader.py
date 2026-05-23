"""Backward-compat shim — the canonical skill subsystem lives in ``deile.skills``.

This module preserves the public API the rest of the codebase (and existing
tests) relies on:

- ``SkillLoader(project_dir, user_home, settings_manager)``
- ``SkillDefinition`` (alias of ``deile.skills.base.Skill``)
- ``_normalize_name``, ``_parse_skill_file`` (private helpers used by tests)

All real work — parsing, discovery, slash-command bridging — is delegated to
``deile.skills``. Add new logic there, not here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..skills.base import Skill as _Skill
from ..skills.discovery import discover_skills_sync
from ..skills.loader import (
    _FRONTMATTER_RE,  # noqa: F401 — re-exported for legacy callers
    _VALID_NAME_RE,  # noqa: F401 — re-exported for legacy callers
    _list_md_files,  # noqa: F401 — re-exported for legacy callers
    normalize_name as _normalize_name,  # noqa: F401 — re-exported for legacy callers
    parse_skill_text,
)
from ..skills.registry import get_skill_registry
from ..skills.slash_command_bridge import (
    register_skills_as_commands,
    unregister_skill_commands,
)

if TYPE_CHECKING:
    from .settings_manager import SettingsManager

logger = logging.getLogger(__name__)


# Backward-compat alias. Existing code (and tests in
# ``deile/tests/test_skill_loader.py``) import ``SkillDefinition`` directly.
SkillDefinition = _Skill


def _parse_skill_file(
    path: Path,
    source: str,
    force_uppercase_name: bool = False,
    kind: str = "skill",
) -> Optional[_Skill]:
    """Legacy parser — reads *path* and delegates to the unified text parser.

    Returns None on any recoverable failure (matches pre-unification behavior).
    YAML errors are pre-detected here so the warning lands on THIS module's
    logger, where legacy tests look for it.
    """
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Cannot read skill file %s: %s", path, exc)
        return None
    match = _FRONTMATTER_RE.match(text)
    if match:
        try:
            yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            logger.warning(
                "Skill file %s has invalid YAML front-matter and will be skipped: %s",
                path,
                exc,
            )
            return None
    return parse_skill_text(
        text,
        path,
        source=source,
        kind=kind,
        force_uppercase_name=force_uppercase_name,
    )


class SkillLoader:
    """Legacy entry point — delegates discovery to ``deile.skills``.

    Preserved so existing call sites (and ``DeileAgent.reload_skills``) keep
    working without changes.
    """

    def __init__(
        self,
        project_dir: Optional[Path] = None,
        user_home: Optional[Path] = None,
        settings_manager: "Optional[SettingsManager]" = None,
    ) -> None:
        self._project_dir = project_dir or Path.cwd()
        self._user_home = user_home or Path.home()
        self._settings_manager = settings_manager

    @property
    def user_skills_dir(self) -> Path:
        return self._user_home / ".deile" / "skills"

    @property
    def project_skills_dir(self) -> Path:
        return self._project_dir / ".deile" / "skills"

    @property
    def user_claude_commands_dir(self) -> Path:
        return self._user_home / ".claude" / "commands"

    @property
    def project_claude_commands_dir(self) -> Path:
        return self._project_dir / ".claude" / "commands"

    def _ensure_user_dir(self) -> None:
        """Create ``~/.deile/skills/`` if it doesn't exist."""
        try:
            self.user_skills_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create user skills dir %s: %s", self.user_skills_dir, exc)

    def _extra_paths(self) -> List[Path]:
        if self._settings_manager is None:
            return []
        paths: List[Path] = []
        for extra_path in self._settings_manager.get_all_skills_paths():
            if not extra_path.is_dir():
                logger.warning(
                    "Configured skill path does not exist or is not a directory: %s",
                    extra_path,
                )
            paths.append(extra_path)
        return paths

    def load_skills(self) -> List[_Skill]:
        """Scan supported skill directories and return merged skill list.

        Legacy contract: only user/project/extras are returned — bundled
        specialist skills are NOT exposed via this method, because they
        should not be registered as ``/<name>`` slash commands.
        Use ``deile.skills.discovery.discover_skills_sync`` directly when
        you need the full set (including bundled).
        """
        self._ensure_user_dir()
        all_skills, _overrides = discover_skills_sync(
            project_dir=self._project_dir,
            user_home=self._user_home,
            extra_paths=self._extra_paths(),
        )
        skills = [s for s in all_skills if s.source != "bundled"]
        if skills:
            logger.info(
                "Loaded %d skill(s) from %s",
                len(skills),
                ", ".join(sorted({s.source for s in skills})),
            )
        return skills

    def load_into_registry(self, registry) -> int:
        """Load skills, register slash commands AND populate the unified registry.

        The unified ``SkillRegistry`` gets the FULL set (including bundled
        specialists) so ``SkillRouter`` can auto-trigger on them. The
        command registry only gets non-bundled skills — bundled specialists
        are not invokable via ``/<name>`` by design.
        """
        self._ensure_user_dir()
        all_skills, _overrides = discover_skills_sync(
            project_dir=self._project_dir,
            user_home=self._user_home,
            extra_paths=self._extra_paths(),
        )

        unified = get_skill_registry()
        for skill in all_skills:
            unified.register(skill)

        invocable = [s for s in all_skills if s.source != "bundled"]
        if invocable:
            logger.info(
                "Loaded %d skill(s) from %s",
                len(invocable),
                ", ".join(sorted({s.source for s in invocable})),
            )

        # Pre-filter collisions HERE so the warning lands on THIS module's
        # logger (legacy tests patch sl_mod.logger.warning to capture it).
        filtered: List[_Skill] = []
        collisions: List[str] = []
        for skill in invocable:
            existing = registry.get_command(skill.name)
            if existing is not None and not getattr(existing, "_is_skill_command", False):
                collisions.append(skill.name)
                logger.warning(
                    "Skill %r (from %s) collides with existing command /%s "
                    "(category=%s) — skipping. Built-in commands cannot be "
                    "overridden by a skill file.",
                    skill.name,
                    skill.source_path,
                    existing.name,
                    getattr(existing, "category", "general"),
                )
                continue
            filtered.append(skill)
        if collisions:
            logger.warning(
                "Skipped %d skill(s) due to name collision with existing commands: %s",
                len(collisions),
                ", ".join(sorted(collisions)),
            )
        return register_skills_as_commands(filtered, registry)

    def reload_into_registry(self, registry) -> int:
        """Drop stale skill commands, then re-load fresh ones from disk."""
        removed = unregister_skill_commands(registry)
        logger.debug("Removed %d stale skill command(s) before reload", removed)
        return self.load_into_registry(registry)
