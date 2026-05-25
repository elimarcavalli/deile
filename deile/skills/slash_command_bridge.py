"""Register loaded ``Skill`` objects as ``/<name>`` slash commands.

Bridges the unified ``Skill`` registry to the legacy slash-invocation flow
(PR #41 era). The command registry sees a thin ``SlashCommand`` subclass
whose ``execute`` returns the skill's body as ``content_type="llm_prompt"``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, List

from .base import Skill

logger = logging.getLogger(__name__)


def _make_command(skill: Skill):
    from ..commands.base import (CommandContext, CommandResult, CommandStatus,
                                 SlashCommand)
    from ..config.manager import CommandConfig

    class _SkillCommand(SlashCommand):
        # Marker used by reload paths to find skill commands without touching built-ins.
        _is_skill_command: bool = True

        def __init__(self) -> None:
            cfg = CommandConfig(name=skill.name, description=skill.description)
            super().__init__(cfg)
            self.category = "commands" if skill.kind == "command" else "skills"
            self._skill_body = skill.content

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


def register_skills_as_commands(skills: Iterable[Skill], command_registry: Any) -> int:
    """Register each *skill* as a ``/<name>`` command on *command_registry*.

    Refuses to override existing commands (built-ins and other skills are
    protected — a dropped ``help.md`` cannot hijack ``/help``).
    """
    registered = 0
    collisions: List[str] = []
    for skill in skills:
        existing = command_registry.get_command(skill.name)
        if existing is not None:
            # Re-registration of an existing skill command is a silent no-op.
            if not getattr(existing, "_is_skill_command", False):
                collisions.append(skill.name)
                logger.warning(
                    "Skill %r (from %s) collides with existing command /%s — skipping",
                    skill.name, skill.source_path, existing.name,
                )
            continue
        try:
            command_registry.register_command(_make_command(skill))
            registered += 1
        except Exception as exc:
            logger.warning("Failed to register skill %r as command: %s", skill.name, exc)

    if collisions:
        logger.warning(
            "Skipped %d skill(s) due to name collision with existing commands: %s",
            len(collisions), ", ".join(sorted(collisions)),
        )
    return registered


def unregister_skill_commands(command_registry: Any) -> int:
    """Drop every command marked ``_is_skill_command=True`` from *command_registry*."""
    # Accesses ``_commands`` directly because that is the contract the legacy
    # ``SkillLoader.reload_into_registry`` already used (preserved for parity).
    skill_names = [
        name
        for name, cmd in list(command_registry._commands.items())
        if getattr(cmd, "_is_skill_command", False)
    ]
    for name in skill_names:
        command_registry.unregister_command(name)
    return len(skill_names)
