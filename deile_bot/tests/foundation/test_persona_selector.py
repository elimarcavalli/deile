"""Tests for PersonaSelector."""

from __future__ import annotations

import pytest

from deile_bot._testing import make_channel, make_envelope, make_user
from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.envelope import ChannelScope
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.persona_selector import PersonaSelector
from deile_bot.foundation.settings import (BotSettings, PersonaRule,
                                           PersonaSettings)


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "ps.sqlite")
    await s.init()
    yield s
    await s.close()


def _settings(default="developer", rules=()):
    return BotSettings(personas=PersonaSettings(default=default, rules=list(rules)))


class TestSelector:
    async def test_default_when_no_rules(self, store):
        sel = PersonaSelector(_settings(default="dev"), IdentityResolver(store))
        env = make_envelope()
        u = make_user()
        assert await sel.resolve(env, u, False) == "dev"

    async def test_match_provider_dm_owner(self, store):
        rules = [
            PersonaRule(
                when={"provider": "discord", "scope": "DM", "owner": True},
                use="developer",
            ),
            PersonaRule(when={"provider": "discord", "scope": "GROUP"}, use="host"),
        ]
        sel = PersonaSelector(_settings(rules=rules), IdentityResolver(store))
        env = make_envelope(
            channel=make_channel(provider="discord", scope=ChannelScope.DM)
        )
        u = make_user()
        assert await sel.resolve(env, u, True) == "developer"

    async def test_first_match_wins(self, store):
        rules = [
            PersonaRule(when={"scope": "GROUP"}, use="first"),
            PersonaRule(when={"scope": "GROUP"}, use="second"),
        ]
        sel = PersonaSelector(_settings(rules=rules), IdentityResolver(store))
        env = make_envelope(channel=make_channel(scope=ChannelScope.GROUP))
        assert await sel.resolve(env, make_user(), False) == "first"

    async def test_channel_name_filter(self, store):
        rules = [
            PersonaRule(
                when={"provider": "discord", "channel_name_in": ["geral"]},
                use="host",
            )
        ]
        sel = PersonaSelector(_settings(rules=rules), IdentityResolver(store))
        env = make_envelope(
            channel=make_channel(provider="discord", name="geral", scope=ChannelScope.GROUP)
        )
        assert await sel.resolve(env, make_user(), False) == "host"
        env_other = make_envelope(
            channel=make_channel(provider="discord", name="random", scope=ChannelScope.GROUP)
        )
        assert await sel.resolve(env_other, make_user(), False) == "developer"

    async def test_owner_required(self, store):
        rules = [
            PersonaRule(when={"owner": True}, use="developer"),
            PersonaRule(when={}, use="host"),
        ]
        sel = PersonaSelector(_settings(rules=rules), IdentityResolver(store))
        env = make_envelope()
        assert await sel.resolve(env, make_user(), True) == "developer"
        assert await sel.resolve(env, make_user(), False) == "host"
