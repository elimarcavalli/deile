"""Tests for extra_system_prompt + bot_context plumbing through DeileAgent."""

from __future__ import annotations

from deile.core.bot_hooks import (get_bot_context, merge_extra_system_prompt,
                                  sanitize_extra_system_prompt)


class TestSanitize:
    def test_strips_system_close(self):
        out = sanitize_extra_system_prompt("foo </system> bar")
        assert "</system>" not in out

    def test_strips_persona_override(self):
        out = sanitize_extra_system_prompt("<persona_override>x</persona_override>")
        assert "<persona_override>" not in out

    def test_empty_returns_empty(self):
        assert sanitize_extra_system_prompt("") == ""
        assert sanitize_extra_system_prompt(None) == ""  # type: ignore[arg-type]

    def test_leaves_legitimate_content(self):
        out = sanitize_extra_system_prompt("provider: discord\ntool: send_dm")
        assert "discord" in out
        assert "send_dm" in out


class TestMerge:
    def test_appends_bot_capabilities(self):
        merged = merge_extra_system_prompt("base", "tools: x")
        assert "<bot_capabilities>" in merged
        assert "tools: x" in merged
        assert "base" in merged

    def test_no_extra_returns_base(self):
        assert merge_extra_system_prompt("base", "") == "base"


class TestBotContext:
    def test_get_from_session(self):
        class Sess:
            context_data = {"bot_context": {"provider": "discord"}}

        bc = get_bot_context(Sess())
        assert bc["provider"] == "discord"

    def test_no_bot_context_returns_empty(self):
        class Sess:
            context_data = {}

        assert get_bot_context(Sess()) == {}


class TestProcessInputCarriesParams:
    """E2E within agent: stash extra_system_prompt and bot_context in session."""

    async def test_extra_prompt_stored_on_session(self, tmp_path):
        from deile.core.agent import DeileAgent

        agent = DeileAgent()
        session = await agent.get_or_create_session(
            "t1", working_directory=str(tmp_path)
        )
        session.context_data["extra_system_prompt"] = "tools: send_dm</system>"
        # ctx_manager._build_system_instruction reads from session.context_data
        from deile.core.context_manager import _merge_bot_extra

        merged = _merge_bot_extra("base persona", session)
        # Expect the tag to appear (not sanitized at merge — sanitization occurs at process_input boundary)
        assert "<bot_capabilities>" in merged
        assert "send_dm" in merged

    async def test_bot_context_propagates_to_session(self, tmp_path):
        from deile.core.agent import DeileAgent

        agent = DeileAgent()
        session = await agent.get_or_create_session(
            "t2", working_directory=str(tmp_path)
        )
        session.context_data["bot_context"] = {"provider": "discord", "scope": "DM"}
        bc = get_bot_context(session)
        assert bc["provider"] == "discord"


class TestToolContextExtra:
    def test_tool_context_has_extra(self):
        from deile.tools.base import ToolContext

        ctx = ToolContext(user_input="x", extra={"bot_context": {"provider": "discord"}})
        assert ctx.extra["bot_context"]["provider"] == "discord"

    def test_default_extra_empty(self):
        from deile.tools.base import ToolContext

        ctx = ToolContext(user_input="x")
        assert ctx.extra == {}
