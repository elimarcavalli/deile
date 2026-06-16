"""Tests for the ``list_skills`` and ``invoke_skill`` function-call tools."""

from __future__ import annotations

import pytest

from deile.skills.base import Skill, SkillTrigger
from deile.skills.registry import get_skill_registry, reset_skill_registry
from deile.tools.base import ToolContext
from deile.tools.skill_tools import InvokeSkillTool, ListSkillsTool


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_skill_registry()
    yield
    reset_skill_registry()


def _register(name: str, *, body: str = "body", **kwargs) -> None:
    get_skill_registry().register(
        Skill(
            name=name,
            description=f"{name} desc",
            body=body,
            triggers=SkillTrigger(**kwargs),
        )
    )


@pytest.mark.unit
class TestListSkillsTool:
    async def test_empty_registry_returns_empty_list(self) -> None:
        tool = ListSkillsTool()
        result = await tool.execute(ToolContext(user_input="", parsed_args={}))
        assert result.is_success
        assert result.data == {"skills": []}

    async def test_lists_every_registered_skill(self) -> None:
        _register("python", file_globs=["*.py"])
        _register("tdd", keywords=["tdd"])
        tool = ListSkillsTool()

        result = await tool.execute(ToolContext(user_input="", parsed_args={}))
        assert result.is_success
        names = [entry["name"] for entry in result.data["skills"]]
        assert sorted(names) == ["python", "tdd"]

    async def test_message_is_markdown_bullet_list(self) -> None:
        _register("python", file_globs=["*.py"])
        tool = ListSkillsTool()

        result = await tool.execute(ToolContext(user_input="", parsed_args={}))
        assert "`python`" in result.message

    async def test_schema_exposes_no_params(self) -> None:
        tool = ListSkillsTool()
        # JSON-Schema-shaped object with empty properties.
        assert tool.schema.parameters.get("properties") == {}
        assert tool.schema.required == []


@pytest.mark.unit
class TestInvokeSkillTool:
    async def test_returns_body_when_skill_exists(self) -> None:
        _register("python", body="USE PEP 8")
        tool = InvokeSkillTool()

        result = await tool.execute(
            ToolContext(user_input="", parsed_args={"name": "python"})
        )
        assert result.is_success
        assert result.message == "USE PEP 8"
        assert result.data["body"] == "USE PEP 8"
        assert result.data["name"] == "python"

    async def test_returns_error_when_skill_missing(self) -> None:
        _register("python")
        tool = InvokeSkillTool()

        result = await tool.execute(
            ToolContext(user_input="", parsed_args={"name": "ghost"})
        )
        assert result.is_error
        assert "ghost" in result.message
        # The error lists what IS available so the LLM can recover.
        assert "python" in result.message

    async def test_returns_error_when_name_empty(self) -> None:
        tool = InvokeSkillTool()
        result = await tool.execute(
            ToolContext(user_input="", parsed_args={"name": ""})
        )
        assert result.is_error
        assert "non-empty" in result.message.lower()

    async def test_returns_error_when_name_missing(self) -> None:
        tool = InvokeSkillTool()
        result = await tool.execute(ToolContext(user_input="", parsed_args={}))
        assert result.is_error

    async def test_strips_whitespace_around_name(self) -> None:
        _register("python", body="X")
        tool = InvokeSkillTool()

        result = await tool.execute(
            ToolContext(user_input="", parsed_args={"name": "  python  "})
        )
        assert result.is_success
        assert result.data["name"] == "python"

    async def test_schema_requires_name_parameter(self) -> None:
        tool = InvokeSkillTool()
        assert tool.schema.required == ["name"]
        assert "name" in tool.schema.parameters["properties"]


@pytest.mark.unit
class TestInvokeSkillErrorCapping:
    async def test_error_truncates_long_name_list(self) -> None:
        reg = get_skill_registry()
        for i in range(50):
            reg.register(Skill(name=f"skill-{i:03d}", description="d", body="b"))

        tool = InvokeSkillTool()
        result = await tool.execute(
            ToolContext(user_input="", parsed_args={"name": "does-not-exist"})
        )
        assert result.is_error
        # Cap is 25; message must indicate the cut-off.
        assert "more" in result.message
        assert "list_skills" in result.message

    async def test_error_below_cap_shows_full_list(self) -> None:
        _register("alpha")
        _register("beta")
        tool = InvokeSkillTool()
        result = await tool.execute(
            ToolContext(user_input="", parsed_args={"name": "gamma"})
        )
        assert result.is_error
        assert "alpha" in result.message
        assert "beta" in result.message
        assert "more" not in result.message


@pytest.mark.unit
class TestAutoDiscovery:
    def test_tools_appear_in_DEFAULT_TOOL_PACKAGES(self) -> None:
        # Guard against accidental removal — if these aren't auto-discovered,
        # the LLM can't call them.
        from deile.tools.discovery import DEFAULT_TOOL_PACKAGES

        assert "deile.tools.skill_tools" in DEFAULT_TOOL_PACKAGES
