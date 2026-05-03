"""Tests for IdentityResolver."""

from __future__ import annotations

import pytest

from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.identity import IdentityResolver, _generate_ulid


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "id.sqlite")
    await s.init()
    yield s
    await s.close()


@pytest.fixture
async def resolver(store):
    return IdentityResolver(store)


class TestUlid:
    def test_ulid_length_26(self):
        ulid = _generate_ulid()
        assert len(ulid) == 26

    def test_ulid_unique(self):
        a = _generate_ulid()
        b = _generate_ulid()
        assert a != b


class TestResolve:
    async def test_first_resolve_creates(self, resolver, store):
        u = await resolver.resolve("discord", "abc", "alice")
        assert u.bot_user_id
        assert u.display_name == "alice"

    async def test_second_resolve_returns_same_id(self, resolver):
        u1 = await resolver.resolve("discord", "abc", "alice")
        u2 = await resolver.resolve("discord", "abc", "alice2")
        assert u1.bot_user_id == u2.bot_user_id

    async def test_display_name_change_preserves_id(self, resolver, store):
        u1 = await resolver.resolve("discord", "abc", "alice")
        u2 = await resolver.resolve("discord", "abc", "alice_new")
        assert u1.bot_user_id == u2.bot_user_id
        assert u2.display_name == "alice_new"

    async def test_int_user_id_normalized_to_str(self, resolver):
        u1 = await resolver.resolve("discord", 12345, "alice")  # type: ignore[arg-type]
        u2 = await resolver.resolve("discord", "12345", "alice")
        assert u1.bot_user_id == u2.bot_user_id

    async def test_is_bot_flag_propagates(self, resolver):
        u = await resolver.resolve("discord", "bot1", "DEILE", is_bot=True)
        assert u.is_bot is True

    async def test_empty_provider_user_id_raises(self, resolver):
        with pytest.raises(ValueError):
            await resolver.resolve("discord", "", "alice")
