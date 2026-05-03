"""Tests for CapabilityCatalog."""

from __future__ import annotations

from deile_bot._testing import FakeAgentMetaProvider, FakeProviderAdapter
from deile_bot.foundation.agent_meta import ToolMeta
from deile_bot.foundation.capabilities import CapabilityCatalog
from deile_bot.foundation.output_formatter import PlainTextFormatter


class TestSnapshot:
    async def test_snapshot_combines_provider_and_agent(self):
        adapter = FakeProviderAdapter()
        agent = FakeAgentMetaProvider()
        cat = CapabilityCatalog()
        snap = await cat.snapshot(adapter, agent, cogs_or_handlers=["help", "ping"])
        assert snap.provider == "fake"
        assert "help" in snap.cogs_or_handlers
        names = {t.name for t in snap.agent_tools}
        assert {"echo", "lookup"} <= names
        assert "deepseek:deepseek-chat" in snap.agent_models
        assert snap.persona_default == "developer"


class TestRenderForSystemPrompt:
    async def test_contains_tool_names(self):
        adapter = FakeProviderAdapter()
        agent = FakeAgentMetaProvider(
            tools=[ToolMeta(name="lookup", description="finds X")]
        )
        cat = CapabilityCatalog()
        snap = await cat.snapshot(adapter, agent)
        rendered = cat.render_for_system_prompt(snap)
        assert "<bot_capabilities>" in rendered
        assert "lookup" in rendered
        assert "fake" in rendered

    async def test_strict_open_close_tags(self):
        adapter = FakeProviderAdapter()
        agent = FakeAgentMetaProvider()
        cat = CapabilityCatalog()
        snap = await cat.snapshot(adapter, agent)
        rendered = cat.render_for_system_prompt(snap)
        assert rendered.startswith("<bot_capabilities>")
        assert rendered.rstrip().endswith("</bot_capabilities>")


class TestRenderForUser:
    async def test_user_render_via_plaintext(self):
        adapter = FakeProviderAdapter()
        agent = FakeAgentMetaProvider()
        cat = CapabilityCatalog()
        snap = await cat.snapshot(adapter, agent)
        rendered = cat.render_for_user(snap, PlainTextFormatter())
        assert "DEILE" in rendered
        assert "Tools" in rendered
