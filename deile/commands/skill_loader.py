"""Backward-compat shim — the canonical skill subsystem lives in ``deile.skills``.

Preserves the legacy public API: ``SkillLoader``, ``SkillDefinition``,
and the private helpers (``_normalize_name``, ``_parse_skill_file``,
``_VALID_NAME_RE``, ``_FRONTMATTER_RE``, ``_list_md_files``) that existing
tests import. All real work delegates to ``deile.skills``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from ..skills.base import Skill as _Skill
from ..skills.discovery import discover_skills_sync
from ..skills.loader import (  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers
    _FRONTMATTER_RE,
    _VALID_NAME_RE,
    _list_md_files,
)
from ..skills.loader import (  # noqa: F401 — re-exported for legacy callers
    normalize_name as _normalize_name,
)
from ..skills.loader import (  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers  # noqa: F401 — re-exported for legacy callers
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


# Legacy alias — tests import ``SkillDefinition`` directly.
SkillDefinition = _Skill


def _parse_skill_file(
    path: Path,
    source: str,
    force_uppercase_name: bool = False,
    kind: str = "skill",
) -> Optional[_Skill]:
    """Legacy parser — delegates to the unified text parser.

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
    """Legacy entry point — delegates discovery to ``deile.skills``."""

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
        try:
            self.user_skills_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(
                "Cannot create user skills dir %s: %s", self.user_skills_dir, exc
            )

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

    def _discover_all(self) -> List[_Skill]:
        self._ensure_user_dir()
        all_skills, _ = discover_skills_sync(
            project_dir=self._project_dir,
            user_home=self._user_home,
            extra_paths=self._extra_paths(),
        )
        return all_skills

    def load_skills(self) -> List[_Skill]:
        """Return non-bundled skills only (legacy contract).

        Bundled specialists are not exposed via this method — they should not
        be registered as ``/<name>`` slash commands. Use
        ``deile.skills.discovery.discover_skills_sync`` for the full set.
        """
        skills = [s for s in self._discover_all() if s.source != "bundled"]
        if skills:
            logger.info(
                "Loaded %d skill(s) from %s",
                len(skills),
                ", ".join(sorted({s.source for s in skills})),
            )
        return skills

    def load_into_registry(self, registry) -> int:
        """Populate the unified registry AND register slash commands.

        The unified ``SkillRegistry`` gets the FULL set (including bundled
        specialists) so ``SkillRouter`` can auto-trigger on them. The
        command registry only gets non-bundled skills — bundled specialists
        are not invokable via ``/<name>`` by design.
        """
        all_skills = self._discover_all()

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

        # Pre-filter collisions HERE so warnings land on THIS module's logger
        # (legacy tests patch sl_mod.logger.warning to capture them).
        filtered: List[_Skill] = []
        collisions: List[str] = []
        for skill in invocable:
            existing = registry.get_command(skill.name)
            if existing is not None and not getattr(
                existing, "_is_skill_command", False
            ):
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
