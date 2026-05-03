"""DiscordAdapter â€” discord.Client wrapped as a foundation ProviderAdapter."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from deile_bot.foundation.envelope import Attachment, BotUser, Channel
from deile_bot.foundation.exceptions import (CapabilityNotSupported,
                                             ProviderError)
from deile_bot.foundation.logging import get_logger
from deile_bot.providers.base import InboundCallback, ProviderAdapter
from deile_bot.providers.discord.intents import build_intents
from deile_bot.providers.discord.normalizer import DiscordNormalizer
from deile_bot.providers.discord.settings import (DISCORD_CAPABILITIES,
                                                  DiscordBotSettings)


class DiscordAdapter(ProviderAdapter):
    name = "discord"
    capabilities = DISCORD_CAPABILITIES

    def __init__(
        self,
        settings: DiscordBotSettings,
        on_inbound: Optional[InboundCallback] = None,
        *,
        runtime: Optional[Any] = None,
    ):
        self.settings = settings
        self.on_inbound = on_inbound
        self.normalizer = DiscordNormalizer()
        self._client: Any = None
        self._self_user_id: str = settings.self_user_id or ""
        self._logger = get_logger("discord.adapter")
        self.runtime: Optional[Any] = runtime

    @property
    def self_user_id(self) -> str:
        return self._self_user_id

    async def start(self) -> None:
        token = self.settings.token.get_secret_value()
        if not token:
            raise ProviderError(
                "DISCORD_TOKEN not configured (env DEILE_BOT_DISCORD_TOKEN)",
                context={},
            )
        # Lazy import to avoid hard dep when the package is unused.
        import discord
        from discord.ext import commands

        intents = build_intents(self.settings)
        client = commands.Bot(
            command_prefix=self.settings.command_prefix,
            intents=intents,
            help_command=None,
        )
        self._client = client

        adapter_ref = self

        @client.event
        async def on_ready():  # noqa: D401
            self._self_user_id = str(client.user.id) if client.user else ""
            self._logger.info(
                "discord ready",
                extra={
                    "user": str(getattr(client.user, "name", "")),
                    "user_id": self._self_user_id,
                },
            )
            try:
                from deile_bot.providers.discord.cogs.help_cog import HelpCog
                from deile_bot.providers.discord.cogs.ping_cog import PingCog

                cog_names = [c.__class__.__name__ for c in client.cogs.values()]
                if "HelpCog" not in cog_names:
                    await client.add_cog(HelpCog(client))
                if "PingCog" not in cog_names:
                    await client.add_cog(PingCog(client))
                if self.runtime is not None:
                    from deile_bot.providers.discord.cogs.agent_cog import \
                        AgentCog
                    from deile_bot.providers.discord.cogs.capabilities_cog import \
                        CapabilitiesCog
                    from deile_bot.providers.discord.cogs.reaction_cog import \
                        ReactionCog

                    if "AgentCog" not in cog_names:
                        await client.add_cog(AgentCog(client, self.runtime, self))
                    if "CapabilitiesCog" not in cog_names:
                        await client.add_cog(CapabilitiesCog(client, self.runtime, self))
                    if "ReactionCog" not in cog_names:
                        await client.add_cog(ReactionCog(client, self.runtime, self))
                if self.settings.slash_sync_guild_ids:
                    for gid in self.settings.slash_sync_guild_ids:
                        await client.tree.sync(guild=discord.Object(id=gid))
                else:
                    await client.tree.sync()
            except Exception:
                self._logger.exception("slash sync failed")

        @client.event
        async def on_message(message):
            if message.author == client.user:
                return
            if adapter_ref.on_inbound is None:
                return
            try:
                env = adapter_ref.normalizer.to_envelope(message)
                await adapter_ref.on_inbound(env, adapter_ref)
            except Exception:
                self._logger.exception("on_message handler raised")
            await client.process_commands(message)

        await client.start(token)

    async def stop(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                self._logger.exception("client.close raised")

    async def send_message(
        self,
        channel: Channel,
        text: str,
        reply_to: Optional[str] = None,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if self._client is None:
            raise ProviderError("Discord client not started", context={})
        try:
            ch = self._client.get_channel(int(channel.provider_channel_id))
            if ch is None:
                ch = await self._client.fetch_channel(int(channel.provider_channel_id))
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"channel lookup failed: {e}", context={}) from e
        ref = None
        if reply_to:
            try:
                import discord

                ref = discord.MessageReference(
                    message_id=int(reply_to),
                    channel_id=ch.id,
                    fail_if_not_exists=False,
                )
            except Exception:
                ref = None
        try:
            msg = await ch.send(content=text, reference=ref, mention_author=False)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"send failed: {e}", context={}) from e
        return str(msg.id)

    async def edit_message(self, channel: Channel, message_id: str, new_text: str) -> None:
        if not self.capabilities.can_edit_message:
            raise CapabilityNotSupported(f"{self.name} cannot edit messages")
        if self._client is None:
            raise ProviderError("Discord client not started", context={})
        try:
            ch = self._client.get_channel(int(channel.provider_channel_id))
            if ch is None:
                ch = await self._client.fetch_channel(int(channel.provider_channel_id))
            msg = await ch.fetch_message(int(message_id))
            await msg.edit(content=new_text)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"edit failed: {e}", context={}) from e

    async def react(self, channel: Channel, message_id: str, emoji: str) -> None:
        if not self.capabilities.can_react:
            raise CapabilityNotSupported(f"{self.name} cannot react")
        if self._client is None:
            raise ProviderError("Discord client not started", context={})
        try:
            ch = self._client.get_channel(int(channel.provider_channel_id))
            if ch is None:
                ch = await self._client.fetch_channel(int(channel.provider_channel_id))
            msg = await ch.fetch_message(int(message_id))
            await msg.add_reaction(emoji)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"react failed: {e}", context={}) from e

    async def send_dm(
        self,
        user: BotUser,
        text: str,
        attachments: Sequence[Attachment] = (),
    ) -> str:
        if not self.capabilities.can_send_dm:
            raise CapabilityNotSupported(f"{self.name} cannot send DM")
        if self._client is None:
            raise ProviderError("Discord client not started", context={})
        try:
            target = await self._client.fetch_user(int(user.provider_user_id))
            msg = await target.send(content=text)
            return str(msg.id)
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"DM send failed: {e}", context={}) from e

    async def fetch_user_profile(self, user: BotUser) -> Mapping[str, Any]:
        if not self.capabilities.can_fetch_user_profile:
            raise CapabilityNotSupported(f"{self.name} cannot fetch profile")
        if self._client is None:
            raise ProviderError("Discord client not started", context={})
        try:
            u = await self._client.fetch_user(int(user.provider_user_id))
            return {
                "name": getattr(u, "name", ""),
                "global_name": getattr(u, "global_name", None),
                "id": str(u.id),
                "avatar_url": str(u.display_avatar.url) if getattr(u, "display_avatar", None) else None,
                "bot": bool(getattr(u, "bot", False)),
            }
        except Exception as e:  # noqa: BLE001
            raise ProviderError(f"fetch_user failed: {e}", context={}) from e

    async def send_typing(self, channel: Channel) -> None:
        if not self.capabilities.can_send_typing:
            return
        if self._client is None:
            return
        try:
            ch = self._client.get_channel(int(channel.provider_channel_id))
            if ch is None:
                ch = await self._client.fetch_channel(int(channel.provider_channel_id))
            # discord.py 2.x: trigger_typing() was removed; ch.typing() is an
            # async context manager that fires the typing indicator. Entering
            # and exiting immediately fires it once (valid for ~10s server-side).
            # Works for TextChannel, DMChannel, Thread, GroupChannel.
            async with ch.typing():
                pass
        except Exception:
            self._logger.warning("send_typing failed", exc_info=True)
