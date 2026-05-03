"""One-shot: list every guild + text channel the bot can see and exit.

Used to discover the channel_id for 'Ministério dos Tokens' before
starting the daemon. Doesn't keep the connection alive.
"""

from __future__ import annotations

import asyncio
import os

import discord
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv()
    token = os.environ["DEILE_BOT_DISCORD_TOKEN"]

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.guilds = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        print(f"== bot: {client.user} ({client.user.id}) ==")
        for g in client.guilds:
            print(f"\nguild: {g.name!r}  id={g.id}")
            for ch in g.channels:
                kind = type(ch).__name__
                print(f"  [{kind}] {getattr(ch, 'name', '?')!r}  id={ch.id}")
        await client.close()

    await client.start(token)


if __name__ == "__main__":
    asyncio.run(main())
