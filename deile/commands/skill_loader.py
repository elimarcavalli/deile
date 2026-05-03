"""Skill loader — discovers .md skill files and registers them as slash commands.

Scans two directories in order (user skills first, project skills second):
  • ~/.deile/skills/   — per-user skills (created automatically if absent)
  • <cwd>/.deile/skills/ — per-project skills (optional, not auto-created)

Project skills take priority: if a user skill and a project skill share the
same name the project skill wins and the user skill is silently dropped.

Skill file format (YAML front-matter + Markdown body):
  ---
  name: my-skill
  description: One-line description shown in /help and autocomplete
  ---
  Prompt body sent to the LLM when the skill is invoked.

The ``name`` field is optional; when absent the stem of the file name is used
(spaces/underscores replaced with hyphens, lowercased).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n", re.DOTALL)
_VALID_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,63}$")


@dataclass
class SkillDefinition:
    """Parsed representation of a single skill file."""

    name: str
    description: str
    body: str
    source: str  # "user" | "project"
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


def _parse_skill_file(path: Path, source: str) -> Optional[SkillDefinition]:
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
            if isinstance(parsed, dict):
                frontmatter_data = {str(k): str(v) for k, v in parsed.items()}
        except Exception as exc:
            logger.warning("Invalid YAML front-matter in %s: %s", path, exc)
        body = text[match.end():]

    raw_name = frontmatter_data.get("name", "") or _name_from_stem(path.stem)
    name = _normalize_name(raw_name)

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
    ) -> None:
        self._project_dir = project_dir or Path.cwd()
        self._user_home = user_home or Path.home()

    @property
    def user_skills_dir(self) -> Path:
        return self._user_home / ".deile" / "skills"

    @property
    def project_skills_dir(self) -> Path:
        return self._project_dir / ".deile" / "skills"

    def _ensure_user_dir(self) -> None:
        """Create ~/.deile/skills/ if it doesn't exist."""
        try:
            self.user_skills_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Cannot create user skills dir %s: %s", self.user_skills_dir, exc)

    def load_skills(self) -> List[SkillDefinition]:
        """Scan both skill directories and return merged skill list.

        Project skills override user skills when names collide.
        """
        self._ensure_user_dir()

        # Gather user skills first (lower priority)
        merged: Dict[str, SkillDefinition] = {}
        for skill in self._scan_directory(self.user_skills_dir, source="user"):
            merged[skill.name] = skill

        # Project skills override user skills
        for skill in self._scan_directory(self.project_skills_dir, source="project"):
            if skill.name in merged:
                logger.debug(
                    "Project skill %r overrides user skill from %s",
                    skill.name,
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

    def _scan_directory(self, directory: Path, source: str) -> List[SkillDefinition]:
        if not directory.is_dir():
            return []

        skills: List[SkillDefinition] = []
        for md_file in sorted(directory.glob("*.md")):
            skill = _parse_skill_file(md_file, source)
            if skill is not None:
                skills.append(skill)

        return skills

    def load_into_registry(self, registry) -> int:
        """Load skills and register them in *registry*.

        Returns:
            Number of skills registered.
        """
        from .base import CommandContext, CommandResult, CommandStatus, SlashCommand
        from ..config.manager import CommandConfig

        skills = self.load_skills()

        registered = 0
        for skill in skills:
            # Capture skill in closure
            def _make_command(sk: SkillDefinition) -> SlashCommand:
                class _SkillCommand(SlashCommand):
                    def __init__(self) -> None:
                        cfg = CommandConfig(
                            name=sk.name,
                            description=sk.description,
                        )
                        super().__init__(cfg)
                        self.category = "skills"
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

        return registered
