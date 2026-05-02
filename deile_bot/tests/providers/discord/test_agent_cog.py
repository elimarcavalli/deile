"""Tests for AgentCog/CapabilitiesCog/ReactionCog (envelope construction logic)."""

from __future__ import annotations

from types import SimpleNamespace


class _FakeChannel:
    """Plain class — survives isinstance(_, discord.DMChannel) returning False."""

    def __init__(self, cid, name=None, parent=None):
        self.id = cid
        self.name = name
        self.parent = parent


class TestAgentCogEnvelopeShape:
    def test_force_respond_set(self):
        from deile_bot.providers.discord.cogs.agent_cog import AgentCog

        cog = AgentCog.__new__(AgentCog)
        cog.bot = None
        cog.runtime = None
        cog.adapter = None
        ctx = SimpleNamespace(
            channel=_FakeChannel(123, name="general"),
            author=SimpleNamespace(id=42, display_name="alice", name="alice", bot=False),
            message=SimpleNamespace(id=999),
        )
        env = cog._make_envelope(ctx, "diga oi")
        assert env.has_force_respond
        assert env.text == "diga oi"
        assert env.author.provider_user_id == "42"
        assert env.channel.scope.value == "GROUP"
