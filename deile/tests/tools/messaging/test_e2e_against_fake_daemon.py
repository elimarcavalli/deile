"""E2E test — agent calls a messaging tool against a real aiohttp daemon.

We stand up the *real* `ControlPlaneServer` wired to a tiny fake
adapter, hit it via the *real* `BotControlClient`, through the *real*
`BotClientFacade`, executed by the *real* `DiscordSendMessageTool`.

Only the discord-side adapter is faked — every other layer is the
production code that ships with the PR. This catches contract drift
end-to-end.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

# O pacote deilebot mora em repo separado (elimarcavalli/deilebot). Sem ele
# instalado, este módulo não pode importar — o importorskip evita que o
# pytest aborte a collection da suite inteira.
pytest.importorskip("deilebot.foundation.capabilities")
pytest.importorskip("deilebot.runtime.control_plane")

from deilebot.foundation.capabilities import ProviderCapabilities  # noqa: E402
from deilebot.foundation.envelope import AttachmentKind  # noqa: E402
from deilebot.runtime.control_plane import ControlPlaneServer  # noqa: E402
from deilebot.runtime.control_plane import ControlPlaneSettings  # noqa: E402

from deile.integrations.bot import get_bot_client  # noqa: E402
from deile.integrations.bot import reset_bot_client  # noqa: E402
from deile.integrations.bot.config import \
    reset_bot_settings_cache  # noqa: E402
from deile.tools.base import ToolContext  # noqa: E402
from deile.tools.messaging import DiscordSendMessageTool  # noqa: E402

pytestmark = pytest.mark.integration


class FakeAdapter:
    name = "discord"
    capabilities = ProviderCapabilities(
        can_edit_message=True,
        can_react=True,
        can_send_dm=True,
        can_threads=True,
        can_polls=False,
        can_inline_keyboards=False,
        can_slash_commands=True,
        can_voice_messages=False,
        can_send_typing=True,
        can_fetch_user_profile=True,
        has_conversation_window=True,
        max_message_chars=2000,
        max_attachments_per_message=10,
        supported_attachment_kinds=frozenset({AttachmentKind.IMAGE, AttachmentKind.FILE}),
    )
    _client = object()

    def __init__(self):
        self.outbound = []

    async def send_message(self, channel, text, reply_to=None, attachments=()):
        self.outbound.append({"channel": channel.provider_channel_id, "text": text})
        return "msg-1"

    async def react(self, channel, message_id, emoji):
        self.outbound.append({"react": (channel.provider_channel_id, message_id, emoji)})

    async def send_dm(self, user, text, attachments=()):
        self.outbound.append({"dm": (user.provider_user_id, text)})
        return "dm-1"


class _AlwaysOnPermissionManager:
    def check_permission(self, **_kw):
        return True


@pytest.fixture
async def real_daemon() -> AsyncIterator[tuple[ControlPlaneServer, FakeAdapter, int]]:
    settings = ControlPlaneSettings(host="127.0.0.1", port=0, auth_token="e2e-token")
    srv = ControlPlaneServer(settings, version="e2e")
    adapter = FakeAdapter()
    srv.register_adapter("discord", adapter)
    port = await srv.start()
    yield srv, adapter, port
    await srv.stop()


async def test_full_loop_post_to_channel(real_daemon, monkeypatch):
    srv, adapter, port = real_daemon
    monkeypatch.setenv("DEILE_BOT_ENDPOINT", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("DEILE_BOT_AUTH_TOKEN", "e2e-token")
    reset_bot_settings_cache()
    reset_bot_client()
    facade = get_bot_client()
    assert facade.is_available

    tool = DiscordSendMessageTool()
    ctx = ToolContext(
        user_input="",
        parsed_args={"channel_id": "1234", "text": "hello from e2e"},
        session_data={"permission_manager": _AlwaysOnPermissionManager()},
    )
    result = await tool.execute(ctx)
    assert result.is_success, result.message
    assert adapter.outbound[0]["channel"] == "1234"
    assert adapter.outbound[0]["text"] == "hello from e2e"
    assert result.data["message_id"] == "msg-1"

    await facade.aclose()
    reset_bot_client()
