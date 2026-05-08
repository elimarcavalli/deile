"""Discord-DM notifier for the autonomous pipeline.

Every meaningful state transition fires a DM to the configured operator
(``DEILE_PIPELINE_NOTIFY_USER_ID``). The notifier is best-effort — failures
log a warning but never abort the pipeline.

Wiring
------
At runtime we rely on ``deile_bot.bridge.dm_tool.send_discord_dm`` (when the
deile-bot package is importable; that's the standard local-dev configuration)
or fall back to the ``deilebot.bridge.dm_tool`` import path for the renamed
package. Either path uses ``DEILE_BOT_DISCORD_TOKEN`` from the environment.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from deile.orchestration.pipeline.constants import PIPELINE_MSG_TRUNCATE_CHARS
from deile.orchestration.pipeline.labels import WORKFLOW_NEW

logger = logging.getLogger(__name__)

_DM_FN: Optional[Callable[[str, str], Awaitable[dict]]] = None


def _resolve_dm_function() -> Optional[Callable[[str, str], Awaitable[dict]]]:
    """Pick the best available DM sender.

    Tries the new ``deilebot.bridge.dm_tool`` first (current package name),
    then falls back to the legacy ``deile_bot.bridge.dm_tool`` for older
    local installs. Returns None if neither is importable — in that case the
    notifier silently no-ops.
    """
    try:
        from deilebot.bridge.dm_tool import send_discord_dm  # type: ignore
        return send_discord_dm
    except Exception:  # noqa: BLE001 — fall back, log later
        pass
    try:
        from deile_bot.bridge.dm_tool import send_discord_dm  # type: ignore
        return send_discord_dm
    except Exception:  # noqa: BLE001
        return None


class DiscordNotifier:
    """Send pipeline-event notifications via Discord DM."""

    def __init__(
        self,
        user_id: Optional[str] = None,
        *,
        dm_fn: Optional[Callable[[str, str], Awaitable[dict]]] = None,
    ) -> None:
        if user_id is not None:
            self.user_id = user_id
        else:
            from deile.config.settings import get_settings

            self.user_id = get_settings().pipeline_notify_user_id or ""
        self._dm_fn = dm_fn

    @property
    def enabled(self) -> bool:
        return bool(self.user_id)

    async def _send(self, text: str) -> None:
        if not self.enabled:
            return
        global _DM_FN
        fn = self._dm_fn
        if fn is None:
            if _DM_FN is None:
                _DM_FN = _resolve_dm_function()
            fn = _DM_FN
        if fn is None:
            logger.debug("no DM function available; skipping notification")
            return
        try:
            await fn(self.user_id, text)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("pipeline DM failed: %s", exc)

    # -- typed event helpers ------------------------------------------

    async def issue_picked_up(self, number: int, title: str, url: str) -> None:
        await self._send(
            f"🔍 **Pipeline pegou para revisar** issue #{number}: {title}\n🔗 {url}"
        )

    async def issue_reviewed(self, number: int, title: str, url: str) -> None:
        await self._send(
            f"✏️ **Issue revisada** #{number}: {title}\n🔗 {url}"
        )

    async def implementation_started(self, number: int, title: str, branch: str) -> None:
        await self._send(
            f"🛠️ **Implementação iniciada** para issue #{number}: {title}\n"
            f"branch: `{branch}`"
        )

    async def implementation_finished(self, number: int, pr_url: Optional[str]) -> None:
        if pr_url:
            await self._send(
                f"✅ **Implementação concluída** issue #{number}\n🔗 PR: {pr_url}"
            )
        else:
            await self._send(
                f"⚠️ **Implementação finalizada sem PR** para issue #{number}"
            )

    async def pr_picked_up(self, number: int, title: str, url: str) -> None:
        await self._send(
            f"🔎 **Pipeline pegou para revisar** PR #{number}: {title}\n🔗 {url}"
        )

    async def pr_reviewed(self, number: int, title: str, url: str, *, merged: bool) -> None:
        verb = "mergeada" if merged else "revisada"
        emoji = "🟣" if merged else "✅"
        await self._send(f"{emoji} **PR #{number} {verb}**: {title}\n🔗 {url}")

    async def issue_auto_classified(self, number: int, title: str, url: str) -> None:
        await self._send(
            f"📋 **Issue auto-classificada** #{number}: {title}\n"
            f"Label `{WORKFLOW_NEW}` adicionado — na fila do pipeline.\n🔗 {url}"
        )

    async def follow_ups_processed(self, pr_number: int, opened: int, skipped: int) -> None:
        await self._send(
            f"🔗 **Stage 4 — PR #{pr_number}**: {opened} issue(s) abertas, {skipped} puladas."
        )

    async def error(self, where: str, detail: str) -> None:
        await self._send(f"⚠️ **Pipeline error** em `{where}`:\n```\n{detail[:PIPELINE_MSG_TRUNCATE_CHARS]}\n```")
