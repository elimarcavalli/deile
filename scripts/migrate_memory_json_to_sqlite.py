"""Migrate legacy `discord_bot/memory.json` to the new SQLite ConversationStore.

Idempotent — re-running won't double-insert (UNIQUE constraint on
(provider, channel, message_id, direction)).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


async def migrate(source: Path, target_db: Path, *, dry_run: bool = False) -> int:
    from deile_bot.foundation.conversation_store import ConversationStore
    from deile_bot.foundation.envelope import (
        BotUser,
        Channel,
        ChannelScope,
        MessageEnvelope,
    )

    if not source.exists():
        print(f"source not found: {source}", file=sys.stderr)
        return 2
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"failed to parse {source}: {e}", file=sys.stderr)
        return 2
    store = ConversationStore(target_db)
    if not dry_run:
        await store.init()
    total = 0
    skipped = 0
    channels: Mapping[str, Any] = data.get("channels", {}) if isinstance(data, dict) else {}
    for cid, ch in channels.items():
        ch_name = ch.get("channel_name") if isinstance(ch, dict) else None
        channel = Channel(
            provider="discord",
            provider_channel_id=str(cid),
            name=ch_name,
            scope=ChannelScope.GROUP,
        )
        if not dry_run:
            await store.upsert_channel(channel)
        for entry in (ch.get("messages") if isinstance(ch, dict) else []) or []:
            try:
                user_id = str(entry.get("user_id") or entry.get("author_id") or "unknown")
                display = entry.get("display_name") or entry.get("user") or "unknown"
                user = BotUser(
                    bot_user_id=f"discord-legacy-{user_id}",
                    provider="discord",
                    provider_user_id=user_id,
                    display_name=display,
                )
                ts = entry.get("timestamp") or entry.get("ts")
                if ts is None:
                    sent = datetime.now(timezone.utc)
                elif isinstance(ts, (int, float)):
                    sent = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                else:
                    try:
                        sent = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    except Exception:
                        sent = datetime.now(timezone.utc)
                msg_id = str(entry.get("message_id") or entry.get("id") or f"mig-{total}")
                env = MessageEnvelope(
                    message_id=msg_id,
                    channel=channel,
                    author=user,
                    sent_at=sent,
                    text=str(entry.get("content") or entry.get("text") or ""),
                    raw=MappingProxyType({"legacy": True, "source": "memory.json"}),
                )
                if not dry_run:
                    await store.upsert_user(user)
                    await store.record_inbound(env)
                    bot_response = entry.get("bot_response")
                    if bot_response:
                        await store.record_outbound(
                            provider="discord",
                            channel=channel,
                            provider_message_id=f"out-{msg_id}",
                            bot_user_id=user.bot_user_id,
                            text=str(bot_response),
                            reply_to=msg_id,
                            sent_at=sent,
                        )
                total += 1
            except Exception as e:
                skipped += 1
                print(f"skipping entry: {e}", file=sys.stderr)
    if not dry_run:
        await store.close()
    print(f"Migrated: {total}, skipped: {skipped} (dry_run={dry_run})")
    return 0


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate memory.json to SQLite")
    parser.add_argument("--source", default="archive/discord_bot_legacy/memory.json")
    parser.add_argument("--target-db", default="data/deile_bot.sqlite")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    return asyncio.run(migrate(Path(args.source), Path(args.target_db), dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
