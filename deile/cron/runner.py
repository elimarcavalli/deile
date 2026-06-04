"""CronRunner — async loop that fires due CronEntries through DEILE.

When a :class:`CronEntry` becomes due, the runner:

1. Loads the entry from :class:`CronStore`.
2. Builds a fresh DEILE turn using ``entry.prompt`` (the user's natural-
   language scheduled instruction).
3. Calls a host-supplied ``fire_callback(entry)`` which is expected
   to invoke the agent and return a short summary string.
4. Persists ``last_fired_at`` / ``next_fire_at`` (recurring) or disables
   (one-shot) and records the summary in ``last_result``.
5. Optionally DMs the result to ``entry.notify_user_id`` via Discord.

The runner is single-instance per host: two CronRunners on the same DB
file would both fire the same entry. For multi-host deployments, gate
with the existing pipeline lockfile pattern.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from deile.cron.constants import (CRON_DM_PROMPT_MAX_CHARS,
                                  CRON_DM_RESULT_MAX_CHARS,
                                  CRON_RESULT_MAX_CHARS,
                                  CRON_STOP_TIMEOUT_SECONDS,
                                  cron_poll_interval_seconds)
from deile.cron.store import CronEntry, CronStore
from deile.security.audit_logger import get_audit_logger

logger = logging.getLogger(__name__)


def _payload_hash(prompt: str) -> str:
    import hashlib
    digest = hashlib.sha256(prompt.encode(), usedforsecurity=False).hexdigest()[:16]
    return f"sha256:{digest}"


FireCallback = Callable[[CronEntry], Awaitable[str]]


class CronRunner:
    """Polls :class:`CronStore` and fires due entries via callback."""

    def __init__(
        self,
        store: CronStore,
        *,
        fire_callback: Optional[FireCallback] = None,
        poll_interval_seconds: int = cron_poll_interval_seconds(),
        notify_dm: Optional[Callable[[str, str], Awaitable[dict]]] = None,
    ) -> None:
        self.store = store
        self.fire_callback = fire_callback
        self.poll_interval_seconds = poll_interval_seconds
        self.notify_dm = notify_dm
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._fired_count = 0

    @staticmethod
    def _audit_best_effort(method_name: str, **kwargs) -> None:
        """Invoke an ``AuditLogger.log_*`` method without propagating failures.

        Audit log writes are best-effort by contract (cron must keep firing
        even if the SQLite audit DB is locked or full); silencing the failure
        with a bare ``pass`` violates principle 6 of the architectural rules,
        so the helper logs at ``debug`` instead with the exception preserved.
        """
        try:
            getattr(get_audit_logger(), method_name)(**kwargs)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.debug("audit %s failed: %s", method_name, exc, exc_info=True)

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def fired_count(self) -> int:
        return self._fired_count

    async def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_forever(), name="cron-runner")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=CRON_STOP_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    async def tick(self) -> int:
        """Run one polling pass — returns number of entries fired."""
        try:
            due = self.store.list_due()
        except Exception:  # noqa: BLE001 — never let the loop die
            logger.exception("cron list_due failed")
            return 0
        fired = 0
        for entry in due:
            try:
                await self._fire(entry)
                fired += 1
                self._fired_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("cron entry %s fire failed: %s", entry.id, exc)
                # Even on error, mark fired so we don't loop on a poison entry.
                self.store.mark_fired(
                    entry.id, when=datetime.now(timezone.utc),
                    result=f"error: {type(exc).__name__}: {exc}"[:CRON_RESULT_MAX_CHARS],
                )
        return fired

    async def _fire(self, entry: CronEntry) -> None:
        cb = self.fire_callback
        if cb is None:
            logger.warning("CronRunner has no fire_callback wired; skipping %s", entry.id)
            self.store.mark_fired(entry.id, result="skipped: no callback")
            self._audit_best_effort(
                "log_cron_skipped",
                entry_id=entry.id,
                name=entry.id,
                reason="no callback",
            )
            return
        self._audit_best_effort(
            "log_cron_fire",
            entry_id=entry.id,
            name=entry.id,
            schedule=getattr(entry, "cron", None),
            payload_hash=_payload_hash(entry.prompt),
        )
        result_summary = await cb(entry)
        self.store.mark_fired(entry.id, result=str(result_summary)[:CRON_RESULT_MAX_CHARS])
        if self.notify_dm and entry.notify_user_id and result_summary:
            try:
                msg = (
                    f"⏰ **Tarefa agendada executada** ({entry.id})\n"
                    f"> {entry.prompt[:CRON_DM_PROMPT_MAX_CHARS]}\n\n"
                    f"**Resultado:** {str(result_summary)[:CRON_DM_RESULT_MAX_CHARS]}"
                )
                await self.notify_dm(entry.notify_user_id, msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("cron DM failed for %s: %s", entry.id, exc)

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            await self.tick()
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
