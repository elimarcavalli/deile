"""Discord-DM notifier for the autonomous pipeline.

Every meaningful state transition fires a DM to the configured operator
(``DEILE_PIPELINE_NOTIFY_USER_ID``). The notifier is best-effort — failures
log a warning but never abort the pipeline.

Wiring
------
At runtime we resolve the DM sender from ``deilebot.bridge.dm_tool.send_discord_dm``.
The ``deilebot`` package must be importable (installed via ``pip install -e .[bot]``
or the local-dev clone described in CLAUDE.md). The sender uses
``DEILE_BOT_DISCORD_TOKEN`` from the environment.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from deile.orchestration.pipeline.constants import PIPELINE_MSG_TRUNCATE_CHARS
from deile.orchestration.pipeline.labels import (WORKFLOW_IMPLEMENTING,
                                                 WORKFLOW_NEW,
                                                 WORKFLOW_REVIEWED)

logger = logging.getLogger(__name__)

_DM_FN: Optional[Callable[[str, str], Awaitable[dict]]] = None


def _resolve_dm_function() -> Optional[Callable[[str, str], Awaitable[dict]]]:
    """Resolve the DM sender from ``deilebot.bridge.dm_tool``.

    Returns None if ``deilebot`` is not importable — in that case the
    notifier silently no-ops.
    """
    try:
        from deilebot.bridge.dm_tool import send_discord_dm  # type: ignore
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
        # Track whether we already warned about missing DM function this session
        # so we only emit the warning once instead of on every notification.
        self._warned_no_dm_fn: bool = False

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
            # Warn once per notifier instance so the operator knows why DMs are absent.
            if not self._warned_no_dm_fn:
                logger.warning(
                    "DiscordNotifier: no DM function available (deilebot not installed or "
                    "DEILE_BOT_ENDPOINT not set); pipeline notifications silenced. "
                    "Install deilebot and set DEILE_BOT_ENDPOINT + DEILE_BOT_AUTH_TOKEN "
                    "to enable Discord notifications."
                )
                self._warned_no_dm_fn = True
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

    async def implementation_parked(self, number: int, reason: str) -> None:
        """Fired when an implementation attempt failed or opened no PR.

        The issue is left parked in ``~workflow:em_implementacao`` (out of every
        stage's candidate set) with NO automatic retry — so this DM is sent at
        most once per attempt, never on a loop. The message is actionable: to
        retry, the operator moves the label back to ``~workflow:revisada``.
        """
        await self._send(
            f"⏸️ **Implementação pausada** — issue #{number}: "
            f"{reason[:PIPELINE_MSG_TRUNCATE_CHARS]}\n"
            f"A issue ficou em `{WORKFLOW_IMPLEMENTING}` (fora da fila, sem "
            f"re-tentativa automática). Para tentar de novo, mova o label de "
            f"volta para `{WORKFLOW_REVIEWED}`."
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

    async def pr_auto_classified(self, number: int, title: str, url: str) -> None:
        await self._send(
            f"🏷️ **PR auto-triaged** #{number}: {title[:100]}\n🔗 {url}"
        )

    async def mention_processed(self, context_url: str, author: str) -> None:
        await self._send(
            f"💬 **Menção de @{author} processada**\n🔗 {context_url}"
        )
