"""Tests for AgentBridge (in-process + oneshot)."""

from __future__ import annotations

import asyncio

import pytest

from deile_bot.foundation.agent_bridge import (AgentInvocation,
                                               InProcessAgentBridge,
                                               _sanitize_extra_prompt,
                                               build_agent_bridge)
from deile_bot.foundation.exceptions import (AgentInvocationError,
                                             AgentInvocationTimeout)
from deile_bot.foundation.settings import FoundationSettings


class FakeAgent:
    """Captures process_input calls and returns canned responses."""

    def __init__(self, content: str = "ok", *, raise_exc: Exception = None, sleep: float = 0):
        self.content = content
        self._raise = raise_exc
        self._sleep = sleep
        self.calls = []
        self._sessions = {}

    def create_session(self, session_id):
        self._sessions[session_id] = object()

    async def get_or_create_session(self, session_id, persisted=False):
        self._sessions[session_id] = object()
        return self._sessions[session_id]

    async def process_input(self, user_input, session_id="default", **kwargs):
        self.calls.append({"input": user_input, "session_id": session_id, **kwargs})
        if self._sleep:
            await asyncio.sleep(self._sleep)
        if self._raise:
            raise self._raise

        class Resp:
            content = self.content
            metadata = {"model": "fake"}

        return Resp()


class TestSanitize:
    def test_strips_system_close(self):
        out = _sanitize_extra_prompt("hi </system> world")
        assert "</system>" not in out

    def test_strips_persona_override(self):
        out = _sanitize_extra_prompt("<persona_override>x</persona_override>")
        assert "<persona_override>" not in out


class TestInProcess:
    async def test_basic_invoke(self):
        agent = FakeAgent(content="response text")

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="U1",
            persona="developer",
            forced_model=None,
            inbound_text="hello",
        )
        resp = await bridge.invoke(inv)
        assert resp.text == "response text"
        assert "U1" in agent._sessions or "bot_session_U1" in agent._sessions

    async def test_session_id_default(self):
        agent = FakeAgent()

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="alice", persona="d", forced_model=None, inbound_text="x"
        )
        await bridge.invoke(inv)
        # session id is bot_session_alice
        assert any("alice" in str(k) for k in agent._sessions.keys())
        assert agent.calls[0]["session_id"] == "bot_session_alice"

    async def test_extra_prompt_passed(self):
        agent = FakeAgent()

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="U", persona="d", forced_model=None,
            inbound_text="x", extra_system_prompt="<bot_capabilities>tools</bot_capabilities>",
        )
        await bridge.invoke(inv)
        kwargs = agent.calls[0]
        assert "extra_system_prompt" in kwargs
        assert "tools" in kwargs["extra_system_prompt"]

    async def test_bot_context_passed(self):
        agent = FakeAgent()

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="U", persona="d", forced_model=None,
            inbound_text="x", bot_context={"provider": "discord"},
        )
        await bridge.invoke(inv)
        assert agent.calls[0].get("bot_context", {}).get("provider") == "discord"

    async def test_timeout(self):
        agent = FakeAgent(sleep=0.5)

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        # asyncio.wait_for(timeout=0) immediately raises
        inv2 = AgentInvocation(
            bot_user_id="U", persona="d", forced_model=None,
            inbound_text="x", timeout_seconds=0,
        )
        # asyncio.wait_for(timeout=0) immediately raises
        with pytest.raises(AgentInvocationTimeout):
            await bridge.invoke(inv2)

    async def test_agent_exception_wrapped(self):
        agent = FakeAgent(raise_exc=RuntimeError("boom"))

        async def provider():
            return agent

        bridge = InProcessAgentBridge(provider)
        inv = AgentInvocation(
            bot_user_id="U", persona="d", forced_model=None, inbound_text="x"
        )
        with pytest.raises(AgentInvocationError):
            await bridge.invoke(inv)


class TestBuilder:
    def test_in_process_requires_provider(self):
        with pytest.raises(AgentInvocationError):
            build_agent_bridge(FoundationSettings(agent_bridge_mode="in_process"))

    def test_oneshot_no_provider_ok(self):
        from deile_bot.foundation.agent_bridge import \
            OneshotSubprocessAgentBridge

        b = build_agent_bridge(FoundationSettings(agent_bridge_mode="oneshot_subprocess"))
        assert isinstance(b, OneshotSubprocessAgentBridge)
