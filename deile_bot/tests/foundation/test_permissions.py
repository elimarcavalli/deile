"""Tests for PermissionGate."""

from __future__ import annotations

import pytest

from deile_bot._testing import make_user
from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.envelope import ChannelScope
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.permissions import Action, PermissionGate
from deile_bot.foundation.settings import (BotSettings, PermissionRule,
                                           PermissionsSettings)


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "p.sqlite")
    await s.init()
    yield s
    await s.close()


def _settings_with(perms: PermissionsSettings) -> BotSettings:
    return BotSettings(permissions=perms)


class TestOwner:
    async def test_owner_always_allowed(self, store):
        owner = make_user(bot_user_id="OWNER")
        settings = _settings_with(PermissionsSettings(owners=["OWNER"]))
        gate = PermissionGate(settings, IdentityResolver(store))
        d = await gate.check(owner, Action.ADMIN_COMMAND, scope=ChannelScope.DM)
        assert d.allowed and d.reason == "owner"

    async def test_blocklist_beats_owner(self, store):
        owner = make_user(bot_user_id="DUAL")
        settings = _settings_with(
            PermissionsSettings(owners=["DUAL"], blocklist=["DUAL"])
        )
        gate = PermissionGate(settings, IdentityResolver(store))
        d = await gate.check(owner, Action.INVOKE_AGENT, scope=ChannelScope.DM)
        assert not d.allowed and d.reason == "blocklist"


class TestAllowlist:
    async def test_wildcard_default_invoke_agent(self, store):
        u = make_user(bot_user_id="X")
        settings = _settings_with(PermissionsSettings(allowlist_invoke_agent=["*"]))
        gate = PermissionGate(settings, IdentityResolver(store))
        d = await gate.check(u, Action.INVOKE_AGENT, scope=ChannelScope.DM)
        assert d.allowed and "allowlist" in d.reason

    async def test_explicit_allowlist(self, store):
        u = make_user(bot_user_id="A")
        settings = _settings_with(PermissionsSettings(allowlist_invoke_agent=["A"]))
        gate = PermissionGate(settings, IdentityResolver(store))
        d = await gate.check(u, Action.INVOKE_AGENT, scope=ChannelScope.DM)
        assert d.allowed

    async def test_not_in_allowlist(self, store):
        u = make_user(bot_user_id="OUT")
        settings = _settings_with(PermissionsSettings(allowlist_invoke_agent=["IN"]))
        gate = PermissionGate(settings, IdentityResolver(store))
        d = await gate.check(u, Action.INVOKE_AGENT, scope=ChannelScope.DM)
        assert not d.allowed


class TestPerAction:
    async def test_owner_only(self, store):
        u = make_user(bot_user_id="X")
        settings = _settings_with(
            PermissionsSettings(
                per_action={"EXECUTE_TOOL": PermissionRule(mode="owner_only")}
            )
        )
        gate = PermissionGate(settings, IdentityResolver(store))
        d = await gate.check(u, Action.EXECUTE_TOOL, scope=ChannelScope.DM)
        assert not d.allowed and d.reason == "owner_only"

    async def test_send_dm_allowlist(self, store):
        u_in = make_user(bot_user_id="OK")
        u_out = make_user(bot_user_id="NO")
        settings = _settings_with(
            PermissionsSettings(
                per_action={"SEND_DM": PermissionRule(mode="allowlist", list=["OK"])}
            )
        )
        gate = PermissionGate(settings, IdentityResolver(store))
        d_in = await gate.check(u_in, Action.SEND_DM, scope=ChannelScope.DM)
        d_out = await gate.check(u_out, Action.SEND_DM, scope=ChannelScope.DM)
        assert d_in.allowed and not d_out.allowed


class TestIsOwner:
    async def test_is_owner_yes(self, store):
        u = make_user(bot_user_id="O")
        settings = _settings_with(PermissionsSettings(owners=["O"]))
        gate = PermissionGate(settings, IdentityResolver(store))
        assert await gate.is_owner(u)

    async def test_is_owner_blocked(self, store):
        u = make_user(bot_user_id="O")
        settings = _settings_with(PermissionsSettings(owners=["O"], blocklist=["O"]))
        gate = PermissionGate(settings, IdentityResolver(store))
        assert not await gate.is_owner(u)
