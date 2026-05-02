"""E2E-8 — capability snapshot rendering."""

from __future__ import annotations

import pytest

from deile_bot._testing import FakeAgentMetaProvider, FakeProviderAdapter
from deile_bot.foundation.agent_meta import ToolMeta
from deile_bot.foundation.capabilities import CapabilityCatalog
from deile_bot.foundation.output_formatter import PlainTextFormatter

pytestmark = pytest.mark.e2e


async def test_e2e8_snapshot_includes_tools_models_persona():
    adapter = FakeProviderAdapter()
    meta = FakeAgentMetaProvider(
        tools=[
            ToolMeta(name="bash_run", description="run a bash command"),
            ToolMeta(name="read_file", description="read a file"),
        ],
        models=["deepseek:deepseek-chat", "anthropic:claude-3-5"],
        personas=["developer", "host"],
    )
    cat = CapabilityCatalog()
    snap = await cat.snapshot(adapter, meta, cogs_or_handlers=["help", "ping"])
    sys_prompt = cat.render_for_system_prompt(snap)
    user_view = cat.render_for_user(snap, PlainTextFormatter())
    assert "bash_run" in sys_prompt
    assert "deepseek:deepseek-chat" in sys_prompt
    assert "developer" in sys_prompt
    assert "DEILE" in user_view
    assert "bash_run" in user_view
    assert len(user_view) <= adapter.capabilities.max_message_chars
