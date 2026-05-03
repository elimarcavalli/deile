from types import SimpleNamespace

import pytest

from deile_bot.foundation.agent_bridge import AgentInvocation, InProcessAgentBridge


class _FakeAgent:
    def __init__(self):
        self.session = SimpleNamespace(context_data={})

    async def get_or_create_session(self, session_id: str, persisted: bool = True):
        return self.session

    def get_session(self, session_id: str):
        return self.session

    async def process_input(self, text: str, **kwargs):
        return SimpleNamespace(
            content="ok",
            metadata={"model_used": "deepseek:deepseek-v4-pro"},
        )


async def _agent_provider(agent):
    return agent


@pytest.mark.unit
async def test_bridge_writes_forced_model_as_locked_override():
    agent = _FakeAgent()
    bridge = InProcessAgentBridge(lambda: _agent_provider(agent))

    response = await bridge.invoke(
        AgentInvocation(
            bot_user_id="u1",
            persona="developer",
            forced_model="deepseek:deepseek-v4-pro",
            inbound_text="oi",
        )
    )

    assert agent.session.context_data["forced_model"] == "deepseek:deepseek-v4-pro"
    assert agent.session.context_data["model_override_locked"] is True
    assert agent.session.context_data["model_override_lock_source"] == "deile_bot"
    assert "preferred_model" not in agent.session.context_data
    assert "_bot_forced_model" not in agent.session.context_data
    assert response.model_used == "deepseek:deepseek-v4-pro"


@pytest.mark.unit
async def test_bridge_writes_default_model_as_soft_preference():
    agent = _FakeAgent()
    bridge = InProcessAgentBridge(lambda: _agent_provider(agent))

    await bridge.invoke(
        AgentInvocation(
            bot_user_id="u1",
            persona="developer",
            forced_model=None,
            default_model="deepseek:deepseek-v4-pro",
            inbound_text="oi",
        )
    )

    assert agent.session.context_data["preferred_model"] == "deepseek:deepseek-v4-pro"
    assert "forced_model" not in agent.session.context_data
    assert "model_override_locked" not in agent.session.context_data


@pytest.mark.unit
async def test_bridge_clears_stale_bot_lock_when_forced_setting_is_absent():
    agent = _FakeAgent()
    agent.session.context_data["forced_model"] = "deepseek:deepseek-v4-pro"
    agent.session.context_data["model_override_locked"] = True
    agent.session.context_data["model_override_lock_source"] = "deile_bot"
    agent.session.context_data["preferred_model"] = "deepseek:deepseek-v4-pro"
    agent.session.context_data["_bot_forced_model"] = "deepseek:deepseek-v4-pro"
    bridge = InProcessAgentBridge(lambda: _agent_provider(agent))

    await bridge.invoke(
        AgentInvocation(
            bot_user_id="u1",
            persona="developer",
            forced_model=None,
            inbound_text="oi",
        )
    )

    assert "forced_model" not in agent.session.context_data
    assert "model_override_locked" not in agent.session.context_data
    assert "model_override_lock_source" not in agent.session.context_data
    assert "preferred_model" not in agent.session.context_data
    assert "_bot_forced_model" not in agent.session.context_data


@pytest.mark.unit
def test_bot_settings_env_model_controls(monkeypatch):
    from deile_bot.foundation.settings import reset_bot_settings_cache, get_bot_settings

    reset_bot_settings_cache()
    monkeypatch.setenv("DEILE_BOT_FORCED_MODEL", "deepseek:deepseek-v4-pro")
    monkeypatch.setenv("DEILE_BOT_DEFAULT_MODEL", "gemini:gemini-2.5-flash-lite")
    try:
        settings = get_bot_settings().foundation
        assert settings.forced_model == "deepseek:deepseek-v4-pro"
        assert settings.default_model == "gemini:gemini-2.5-flash-lite"
    finally:
        reset_bot_settings_cache()
