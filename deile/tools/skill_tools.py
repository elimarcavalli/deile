"""Function-call tools that let the LLM consume skills on demand.

These tools wrap the unified ``deile.skills.SkillRegistry`` — they do not
read disk, parse files, or maintain their own state. Whatever the registry
holds (bundled + user + project + extras, hot-reloaded or not) is what
these tools see.

Two tools are exposed:

- **list_skills** — returns the catalog (names + descriptions + trigger
  hints) so the LLM can discover what's available.
- **invoke_skill(name)** — returns the body of one named skill, suitable
  for the LLM to read and apply.

Together they implement the "the LLM can dynamically pull any skill"
half of the design — complementary to the auto-injection performed by
``SkillRouter`` for triggered skills. Auto-discovery picks both up via
``DEFAULT_TOOL_PACKAGES``.
"""

from __future__ import annotations

import logging
from typing import List

from ..skills.registry import get_skill_registry
from ..skills.router import _trigger_hint
from .base import (
    SecurityLevel,
    Tool,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolSchema,
)

logger = logging.getLogger(__name__)


class ListSkillsTool(Tool):
    """Return the catalog of every skill currently in the registry."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="list_skills",
                description=(
                    "List every skill currently loaded in the DEILE skill "
                    "registry, along with its one-line description and "
                    "trigger hint. Use this to discover what expertise is "
                    "available before calling `invoke_skill`. The skills "
                    "auto-triggered for this turn are already injected "
                    "above as 'Active Skills' — listing them again here "
                    "would be redundant."
                ),
                parameters={
                    "type": "object",
                    "properties": {},
                },
                required=[],
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.OTHER,
            )
        )

    async def execute(self, context: ToolContext) -> ToolResult:
        registry = get_skill_registry()
        skills = sorted(registry.list_all(), key=lambda s: s.name)
        if not skills:
            return ToolResult.success_result(
                data={"skills": []},
                message="No skills are currently loaded.",
            )

        lines: List[str] = []
        catalog: List[dict] = []
        for skill in skills:
            hint = _trigger_hint(skill)
            line = f"- `{skill.name}` — {skill.description}"
            if hint:
                line += f" _(auto-active when {hint[len('auto-active when '):]})_"
            lines.append(line)
            catalog.append({
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
                "trigger_hint": hint,
            })

        return ToolResult.success_result(
            data={"skills": catalog},
            message="\n".join(lines),
        )


class InvokeSkillTool(Tool):
    """Pull the body of a named skill out of the registry."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="invoke_skill",
                description=(
                    "Load the full body of a skill from the DEILE skill "
                    "registry by name. Returns the same Markdown content "
                    "that auto-trigger injection would have prepended to "
                    "the system prompt — call this when a skill listed in "
                    "the catalog applies to the current task but its "
                    "triggers did not fire automatically. Use `list_skills` "
                    "(or the catalog in the system prompt) to discover "
                    "valid names. Returns an error result when the name is "
                    "unknown."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Exact registry name of the skill to load. "
                                "Names are lower-case hyphen-separated "
                                "(e.g. 'python', 'tdd', 'my-skill') except "
                                "for skills loaded from ~/.claude/commands "
                                "which are UPPER-CASE."
                            ),
                        },
                    },
                    "required": ["name"],
                },
                required=["name"],
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.OTHER,
            )
        )

    async def execute(self, context: ToolContext) -> ToolResult:
        name = context.parsed_args.get("name")
        if not isinstance(name, str) or not name.strip():
            return ToolResult.error_result(
                message="invoke_skill requires a non-empty `name` argument.",
            )

        registry = get_skill_registry()
        skill = registry.get(name.strip())
        if skill is None:
            available = ", ".join(registry.list_names()) or "(none loaded)"
            return ToolResult.error_result(
                message=(
                    f"Skill '{name}' is not registered. Available skills: "
                    f"{available}"
                ),
            )

        logger.debug("skill_tools: invoke_skill('%s') served (source=%s)", name, skill.source)
        return ToolResult.success_result(
            data={
                "name": skill.name,
                "description": skill.description,
                "source": skill.source,
                "body": skill.body,
            },
            message=skill.body,
        )
