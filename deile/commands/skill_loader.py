"""Skill loader — discovers .md skill files and registers them as slash commands.

Scans directories in order (user first, project second):
  • ~/.deile/skills/   — per-user skills (created automatically if absent)
  • ~/.claude/commands/ — per-user Claude command skills (optional)
  • <cwd>/.deile/skills/ — per-project skills (optional, not auto-created)
  • <cwd>/.claude/commands/ — per-project Claude command skills (optional)

Later directories take priority: if two skills share the same name, the last
one scanned wins.

Skill file format (YAML front-matter + Markdown body):
  ---
  name: my-skill
  description: One-line description shown in /help and autocomplete
  ---
  Prompt body sent to the LLM when the skill is invoked.

The ``name`` field is optional; when absent the stem of the file name is used
(spaces/underscores replaced with hyphens). Command files from ``commands``
directories are registered with UPPERCASE names.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from .settings_manager import SettingsManager

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,63}$")


@dataclass
class SkillDefinition:
    """Parsed representation of a single skill file."""

    name: str
    description: str
    body: str
    source: str  # "user" | "project"
    kind: str = "skill"  # "skill" | "command"
    file_path: Path = field(default_factory=Path)


def _normalize_name(raw: str) -> str:
    """Lower-case, replace whitespace/underscores with hyphens, strip extras."""
    name = raw.strip().lower()
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"[^a-z0-9\-]", "", name)
    name = name.strip("-")
    return name


def _name_from_stem(stem: str) -> str:
    return _normalize_name(stem)


def _parse_skill_file(
    path: Path,
    source: str,
    force_uppercase_name: bool = False,
    kind: str = "skill",
) -> Optional[SkillDefinition]:
    """Return a SkillDefinition from *path*, or None on unrecoverable error."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Cannot read skill file %s: %s", path, exc)
        return None

    frontmatter_data: Dict[str, str] = {}
    body = text

    match = _FRONTMATTER_RE.match(text)
    if match:
        import yaml  # already in requirements (PyYAML)

        try:
            parsed = yaml.safe_load(match.group(1)) or {}
        except Exception as exc:
            # Loud warning + skip — silently treating malformed YAML as
            # "no frontmatter" (the previous behaviour) hid typos and made
            # debugging painful (see PR #51 review F3).
            logger.warning(
                "Skill file %s has invalid YAML front-matter and will be skipped: %s",
                path,
                exc,
            )
            return None
        else:
            if isinstance(parsed, dict):
                # Per-key strict validation. Previously every value was
                # blanket-coerced via str(), which produced absurd commands
                # (e.g. ``name: null`` -> ``/none``; ``description: [a, b]``
                # -> ``"['a', 'b']"``). Now we accept only string scalars
                # for the keys we care about.
                raw_name = parsed.get("name")
                if raw_name is not None:
                    if not isinstance(raw_name, str):
                        logger.warning(
                            "Skill file %s: front-matter ``name`` must be a string, got %s — using filename stem",
                            path,
                            type(raw_name).__name__,
                        )
                    else:
                        frontmatter_data["name"] = raw_name
                raw_desc = parsed.get("description")
                if raw_desc is not None:
                    if not isinstance(raw_desc, str):
                        logger.warning(
                            "Skill file %s: front-matter ``description`` must be a string, got %s — using default",
                            path,
                            type(raw_desc).__name__,
                        )
                    else:
                        frontmatter_data["description"] = raw_desc
        body = text[match.end():]

    raw_name = frontmatter_data.get("name", "") or _name_from_stem(path.stem)
    name = _normalize_name(raw_name)
    if force_uppercase_name:
        name = name.upper()

    if not _VALID_NAME_RE.match(name):
        logger.warning(
            "Skill file %s produces invalid name %r — skipped", path, name
        )
        return None

    description = frontmatter_data.get("description", "") or f"Skill: {name}"
    body = body.strip()

    if not body:
        logger.warning("Skill file %s has an empty body — skipped", path)
        return None

    return SkillDefinition(
        name=name,
        description=description,
        body=body,
        source=source,
        kind=kind,
        file_path=path,
    )


class SkillLoader:
    """Discovers skill files and produces SkillDefinition objects.

    Args:
        project_dir: Root of the current project (defaults to ``Path.cwd()``).
        user_home:   User's home directory (defaults to ``Path.home()``).
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
        """Create ~/.deile/skills/ if it doesn't exist."""
        try:
            self.user_skills_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create user skills dir %s: %s", self.user_skills_dir, exc)

    def load_skills(self) -> List[SkillDefinition]:
        """Scan supported skill directories and return merged skill list.

        Later directories override earlier ones when names collide.
        """
        self._ensure_user_dir()

        merged: Dict[str, SkillDefinition] = {}
        scan_order = [
            (self.user_skills_dir, "user", False, "skill"),
            (self.user_claude_commands_dir, "user", True, "command"),
            (self.project_skills_dir, "project", False, "skill"),
            (self.project_claude_commands_dir, "project", True, "command"),
        ]

        # Extra paths registered via SettingsManager override the defaults on collision.
        if self._settings_manager is not None:
            for extra_path in self._settings_manager.get_all_skills_paths():
                if not extra_path.is_dir():
                    logger.warning(
                        "Configured skill path does not exist or is not a directory: %s",
                        extra_path,
                    )
                scan_order.append((extra_path, "extra", False, "skill"))

        for directory, source, force_uppercase_name, kind in scan_order:
            for skill in self._scan_directory(
                directory,
                source=source,
                force_uppercase_name=force_uppercase_name,
                kind=kind,
            ):
                if skill.name in merged:
                    logger.debug(
                        "Skill %r from %s overrides previous definition from %s",
                        skill.name,
                        skill.file_path,
                        merged[skill.name].file_path,
                    )
                merged[skill.name] = skill

        skills = list(merged.values())
        if skills:
            logger.info(
                "Loaded %d skill(s) from %s",
                len(skills),
                ", ".join(sorted({s.source for s in skills})),
            )
        return skills

    def _scan_directory(
        self,
        directory: Path,
        source: str,
        force_uppercase_name: bool = False,
        kind: str = "skill",
    ) -> List[SkillDefinition]:
        if not directory.is_dir():
            return []

        skills: List[SkillDefinition] = []
        for md_file in sorted(directory.glob("*.md")):
            skill = _parse_skill_file(
                md_file,
                source,
                force_uppercase_name=force_uppercase_name,
                kind=kind,
            )
            if skill is not None:
                skills.append(skill)

        return skills

    def reload_into_registry(self, registry) -> int:
        """Unregister all skill commands then load fresh ones from disk.

        Skill commands are identified by the ``_is_skill_command`` marker placed
        by ``load_into_registry``. Built-in commands are never touched.
        """
        skill_names = [
            name
            for name, cmd in list(registry._commands.items())
            if getattr(cmd, "_is_skill_command", False)
        ]
        for name in skill_names:
            registry.unregister_command(name)
        logger.debug("Removed %d stale skill command(s) before reload", len(skill_names))
        return self.load_into_registry(registry)

    def load_into_registry(self, registry) -> int:
        """Load skills and register them in *registry*.

        Refuses to register a skill whose name collides with
        a command already in the registry — the built-in ``/help``,
        ``/model``, ``/cost`` etc. must NOT be hijack-able by a dropped
        ``.md`` file (see PR #51 review F1: a malicious project-level
        ``.deile/skills/help.md`` would otherwise silently take over
        ``/help`` for anyone who clones the repo).

        Returns:
            Number of skills registered (skipped collisions are NOT counted).
        """
        from .base import CommandContext, CommandResult, CommandStatus, SlashCommand
        from ..config.manager import CommandConfig

        skills = self.load_skills()

        registered = 0
        skipped_collisions: List[str] = []
        for skill in skills:
            # F1 guard: refuse to override a built-in (or any) existing
            # command.
            existing = registry.get_command(skill.name)
            if existing is not None:
                logger.warning(
                    "Skill %r (from %s) collides with existing command /%s "
                    "(category=%s) — skipping. Built-in commands cannot be "
                    "overridden by a skill file.",
                    skill.name,
                    skill.file_path,
                    existing.name,
                    getattr(existing, "category", "general"),
                )
                skipped_collisions.append(skill.name)
                continue

            # Capture skill in closure
            def _make_command(sk: SkillDefinition) -> SlashCommand:
                class _SkillCommand(SlashCommand):
                    # Marker used by reload_into_registry to identify and remove
                    # skill commands without touching built-ins.
                    _is_skill_command: bool = True

                    def __init__(self) -> None:
                        cfg = CommandConfig(
                            name=sk.name,
                            description=sk.description,
                        )
                        super().__init__(cfg)
                        self.category = "commands" if sk.kind == "command" else "skills"
                        self._skill_body = sk.body

                    async def execute(self, ctx: CommandContext) -> CommandResult:
                        prompt = self._skill_body
                        if ctx.args and ctx.args.strip():
                            prompt = f"{prompt}\n\nArguments: {ctx.args.strip()}"
                        return CommandResult(
                            success=True,
                            content=prompt,
                            content_type="llm_prompt",
                            status=CommandStatus.SUCCESS,
                        )

                return _SkillCommand()

            cmd = _make_command(skill)
            try:
                registry.register_command(cmd)
                registered += 1
            except Exception as exc:
                logger.warning("Failed to register skill %r: %s", skill.name, exc)

        if skipped_collisions:
            logger.warning(
                "Skipped %d skill(s) due to name collision with existing commands: %s",
                len(skipped_collisions),
                ", ".join(sorted(skipped_collisions)),
            )

        return registered
