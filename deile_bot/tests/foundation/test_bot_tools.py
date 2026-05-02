"""Tests for foundation transversal bot tools."""

from __future__ import annotations

import pytest

from deile_bot._testing import FakeProviderAdapter, make_user
from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.conversation_store import ConversationStore
from deile_bot.foundation.envelope import AttachmentKind
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             PermissionDenied)
from deile_bot.foundation.identity import IdentityResolver
from deile_bot.foundation.permissions import PermissionGate
from deile_bot.foundation.settings import (BotSettings, PermissionRule,
                                           PermissionsSettings)
from deile_bot.foundation.tools.base import get_bot_context
from deile_bot.foundation.tools.get_user_profile import GetUserProfileTool
from deile_bot.foundation.tools.react_to_message import ReactToMessageTool
from deile_bot.foundation.tools.send_dm import SendDMTool


class _Ctx:
    def __init__(self, extra=None):
        self.extra = extra or {}


@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "tools.sqlite")
    await s.init()
    yield s
    await s.close()


class TestBase:
    def test_get_bot_context_empty(self):
        ctx = _Ctx()
        assert get_bot_context(ctx) == {}

    def test_get_bot_context_returns_dict(self):
        ctx = _Ctx({"bot_context": {"adapter_ref": "x"}})
        bc = get_bot_context(ctx)
        assert bc["adapter_ref"] == "x"


class TestSendDM:
    async def test_no_context_raises(self):
        tool = SendDMTool()
        with pytest.raises(PermissionDenied):
            await tool.run(_Ctx(), bot_user_id="x", text="hi")

    async def test_success_path(self, store):
        adapter = FakeProviderAdapter()
        owner = make_user(bot_user_id="OWNER", provider_user_id="42", display_name="me")
        await store.upsert_user(owner)
        identity = IdentityResolver(store)
        settings = BotSettings(
            permissions=PermissionsSettings(
                owners=["OWNER"],
                per_action={"SEND_DM": PermissionRule(mode="allowlist", list=["OWNER"])},
            )
        )
        perms = PermissionGate(settings, identity)
        target_user = await identity.resolve("fake", "555", "Bob")
        ctx = _Ctx({
            "bot_context": {
                "adapter_ref": adapter,
                "permissions": perms,
                "invoker_user": owner,
                "identity": identity,
            }
        })
        tool = SendDMTool()
        out = await tool.run(ctx, bot_user_id=target_user.bot_user_id, text="ola")
        assert out["ok"] is True
        assert adapter.dms[-1]["text"] == "ola"

    async def test_not_owner_denied(self, store):
        adapter = FakeProviderAdapter()
        non_owner = make_user(bot_user_id="STRANGER", provider_user_id="99")
        await store.upsert_user(non_owner)
        identity = IdentityResolver(store)
        settings = BotSettings(permissions=PermissionsSettings())
        perms = PermissionGate(settings, identity)
        ctx = _Ctx({
            "bot_context": {
                "adapter_ref": adapter,
                "permissions": perms,
                "invoker_user": non_owner,
                "identity": identity,
            }
        })
        tool = SendDMTool()
        with pytest.raises(PermissionDenied):
            await tool.run(ctx, bot_user_id="X", text="hi")


class TestGetUserProfile:
    async def test_success(self, store):
        adapter = FakeProviderAdapter()
        identity = IdentityResolver(store)
        target = await identity.resolve("fake", "u-1", "alice")
        ctx = _Ctx({"bot_context": {"adapter_ref": adapter, "identity": identity}})
        tool = GetUserProfileTool()
        out = await tool.run(ctx, bot_user_id=target.bot_user_id)
        assert out["ok"] is True
        assert out["profile"]["display_name"] == "alice"


class TestReactToMessage:
    async def test_success(self):
        adapter = FakeProviderAdapter()
        ctx = _Ctx({"bot_context": {"adapter_ref": adapter}})
        tool = ReactToMessageTool()
        out = await tool.run(ctx, channel_id="c1", message_id="m1", emoji="👍")
        assert out["ok"] is True
        assert adapter.reactions[-1]["emoji"] == "👍"


class TestCapabilityNotSupported:
    async def test_react_unsupported_raises(self):
        caps = ProviderCapabilities(
            can_edit_message=False, can_react=False, can_send_dm=False, can_threads=False,
            can_polls=False, can_inline_keyboards=False, can_slash_commands=False,
            can_voice_messages=False, can_send_typing=False, can_fetch_user_profile=False,
            has_conversation_window=False, max_message_chars=100,
            max_attachments_per_message=0,
            supported_attachment_kinds=frozenset({AttachmentKind.IMAGE}),
        )
        adapter = FakeProviderAdapter(capabilities=caps)
        ctx = _Ctx({"bot_context": {"adapter_ref": adapter}})
        tool = ReactToMessageTool()
        with pytest.raises(CapabilityNotSupported):
            await tool.run(ctx, channel_id="c1", message_id="m1", emoji="👍")
