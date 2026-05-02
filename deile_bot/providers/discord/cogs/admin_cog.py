"""AdminCog — owner-only ops: /dlq, /forget, /sessions, /metrics, /audit, /persona."""

from __future__ import annotations

import json
from typing import Any, Optional

from discord import app_commands
from discord.ext import commands


def _is_owner_check(adapter):
    """Build a check that consults PermissionGate.is_owner via runtime."""

    async def check(ctx: commands.Context) -> bool:
        runtime = getattr(adapter, "runtime", None)
        if runtime is None:
            return False
        permissions = runtime.pipeline.permissions
        identity = runtime.pipeline.identity
        user = await identity.resolve(
            "discord", str(ctx.author.id),
            getattr(ctx.author, "display_name", None) or ctx.author.name,
        )
        return await permissions.is_owner(user)

    return check


class AdminCog(commands.Cog):
    def __init__(self, bot: Any, runtime: Any, adapter: Any):
        self.bot = bot
        self.runtime = runtime
        self.adapter = adapter

    async def _is_owner(self, ctx: commands.Context) -> bool:
        return await _is_owner_check(self.adapter)(ctx)

    # -------- DLQ --------
    @commands.hybrid_group(name="dlq", description="DLQ ops (owner only)")
    async def dlq(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("usage: /dlq list|replay|purge")

    @dlq.command(name="list", description="List pending DLQ entries")
    async def dlq_list(self, ctx: commands.Context, provider: Optional[str] = None):
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        from deile_bot.foundation.dlq import DeadLetterQueue

        dlq = DeadLetterQueue(self.runtime.pipeline.store, self.runtime.pipeline.permissions._settings)
        rows = await dlq.list_pending(provider=provider, limit=25)
        await ctx.send(f"```json\n{json.dumps(rows, indent=2, default=str)[:1900]}\n```")

    @dlq.command(name="purge", description="Purge DLQ older than N days")
    async def dlq_purge(self, ctx: commands.Context, older_than_days: int = 30):
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        from deile_bot.foundation.dlq import DeadLetterQueue

        dlq = DeadLetterQueue(self.runtime.pipeline.store, self.runtime.pipeline.permissions._settings)
        removed = await dlq.purge(older_than_days)
        await ctx.send(f"removed {removed} entries")

    # -------- Forget (privacy) --------
    @commands.hybrid_command(name="forget", description="Delete a user's history (owner only)")
    @app_commands.describe(bot_user_id="Target bot_user_id")
    async def forget(self, ctx: commands.Context, bot_user_id: str):
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        store = self.runtime.pipeline.store
        removed = await store.purge_user_messages(bot_user_id)
        await ctx.send(f"deleted {removed} messages for {bot_user_id}")

    # -------- Sessions (DEILE side) --------
    @commands.hybrid_group(name="sessions", description="DEILE session ops (owner only)")
    async def sessions(self, ctx: commands.Context):
        if ctx.invoked_subcommand is None:
            await ctx.send("usage: /sessions list|purge")

    @sessions.command(name="purge", description="Purge sessions older than N days")
    async def sessions_purge(self, ctx: commands.Context, older_than_days: int = 30):
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        from deile.core.session_store import SessionStore
        from deile_bot.foundation.settings import get_bot_settings

        bs = get_bot_settings()
        store = SessionStore(bs.foundation.sessions_sqlite_path)
        await store.init()
        removed = await store.purge_older_than(older_than_days)
        await store.close()
        await ctx.send(f"purged {removed} sessions")

    # -------- Metrics --------
    @commands.hybrid_command(name="metrics", description="Snapshot of MetricsCollector (owner only)")
    async def metrics(self, ctx: commands.Context):
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        snap = self.runtime.pipeline.metrics.snapshot()
        text = json.dumps(snap, indent=2, default=str)
        await ctx.send(f"```json\n{text[:1900]}\n```")

    # -------- Audit --------
    @commands.hybrid_command(name="audit", description="Recent audit entries (owner only)")
    async def audit(self, ctx: commands.Context, limit: int = 25):
        if not await self._is_owner(ctx):
            await ctx.send("⛔ owner only")
            return
        rows = await self.runtime.pipeline.store.query_audit(limit=limit)
        text = json.dumps(rows, indent=2, default=str)
        await ctx.send(f"```json\n{text[:1900]}\n```")
