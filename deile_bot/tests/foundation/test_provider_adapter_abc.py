"""Tests for ProviderAdapter ABC + FakeProviderAdapter behavior."""

from __future__ import annotations

import pytest

from deile_bot._testing import (FakeProviderAdapter, make_channel,
                                make_envelope, make_user)
from deile_bot.foundation.capabilities import ProviderCapabilities
from deile_bot.foundation.envelope import AttachmentKind
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             ProviderError)


def _no_edit_caps() -> ProviderCapabilities:
    return ProviderCapabilities(
        can_edit_message=False,
        can_react=False,
        can_send_dm=False,
        can_threads=False,
        can_polls=False,
        can_inline_keyboards=False,
        can_slash_commands=False,
        can_voice_messages=False,
        can_send_typing=False,
        can_fetch_user_profile=False,
        has_conversation_window=False,
        max_message_chars=1000,
        max_attachments_per_message=0,
        supported_attachment_kinds=frozenset({AttachmentKind.IMAGE}),
    )


class TestFakeAdapter:
    async def test_send_message_appears_in_inbox(self):
        adapter = FakeProviderAdapter()
        ch = make_channel()
        msg_id = await adapter.send_message(ch, "hello")
        assert adapter.inbox[-1]["text"] == "hello"
        assert adapter.inbox[-1]["message_id"] == msg_id

    async def test_send_dm_appears_in_dms(self):
        adapter = FakeProviderAdapter()
        u = make_user()
        await adapter.send_dm(u, "hi")
        assert adapter.dms[-1]["text"] == "hi"

    async def test_react_records(self):
        adapter = FakeProviderAdapter()
        ch = make_channel()
        await adapter.react(ch, "m1", "👍")
        assert adapter.reactions[-1]["emoji"] == "👍"

    async def test_inject_calls_on_inbound(self):
        adapter = FakeProviderAdapter()
        captured: list = []

        async def cb(env, a):
            captured.append((env.message_id, a is adapter))

        adapter.on_inbound = cb
        env = make_envelope(text="ping")
        await adapter.inject(env)
        assert captured[0][1] is True

    async def test_failure_injection(self):
        adapter = FakeProviderAdapter()
        adapter.fail_send_n_times(2)
        ch = make_channel()
        with pytest.raises(ProviderError):
            await adapter.send_message(ch, "x")
        with pytest.raises(ProviderError):
            await adapter.send_message(ch, "y")
        # third succeeds
        msg_id = await adapter.send_message(ch, "z")
        assert msg_id

    async def test_capability_not_supported_for_no_edit(self):
        adapter = FakeProviderAdapter(capabilities=_no_edit_caps())
        ch = make_channel()
        with pytest.raises(CapabilityNotSupported):
            await adapter.edit_message(ch, "1", "x")
        with pytest.raises(CapabilityNotSupported):
            await adapter.react(ch, "1", "x")
        u = make_user()
        with pytest.raises(CapabilityNotSupported):
            await adapter.send_dm(u, "x")

    async def test_typing_silent_when_unsupported(self):
        adapter = FakeProviderAdapter(capabilities=_no_edit_caps())
        ch = make_channel()
        await adapter.send_typing(ch)
        assert adapter.typing_calls == []

    async def test_typing_recorded_when_supported(self):
        adapter = FakeProviderAdapter()
        ch = make_channel(provider_channel_id="abc")
        await adapter.send_typing(ch)
        assert "abc" in adapter.typing_calls


class TestMarkupASTSkeleton:
    def test_from_plain_single_span(self):
        from deile.common.markup_ast import MarkupAST, SpanKind

        ast = MarkupAST.from_plain("hello world")
        assert len(ast) == 1
        assert ast[0].kind == SpanKind.PLAIN
        assert ast[0].text == "hello world"

    def test_from_plain_empty(self):
        from deile.common.markup_ast import MarkupAST

        ast = MarkupAST.from_plain("")
        assert len(ast) == 0

    def test_to_plain_roundtrip(self):
        from deile.common.markup_ast import MarkupAST

        text = "hello world"
        ast = MarkupAST.from_plain(text)
        assert ast.to_plain() == text
