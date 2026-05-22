"""Headless entrypoint that runs the :class:`PipelineMonitor` until killed.

Used by the ``deile-pipeline`` deployment (via the ``pipeline`` role of
``infra/k8s/wrapper.py``) to run the autonomous issue → PR → merge loop as a
long-lived process with **no TTY, CLI or ``rich`` dependency** — it imports
only the monitor + config, never ``deile.cli``.

When ``dispatch_mode == "deile_worker"`` (the product default) the monitor
needs no local LLM provider: it orchestrates via the ``gh`` CLI and dispatches
the heavy implement/review work to the ``deile-worker`` Pod over HTTP. That is
why this runner does **not** call ``bootstrap_providers`` or construct a
``DeileAgent`` — keeping the pipeline Pod lean.
"""

from __future__ import annotations

import asyncio
import logging
import signal

logger = logging.getLogger("deile.pipeline.runner")


def _build_notifier(user_id: str):
    """DiscordNotifier whose DM sender routes through the bot control plane.

    The default :func:`send_discord_dm` path spins up a ``discord.Client`` and
    needs ``DEILE_BOT_DISCORD_TOKEN`` — which the pipeline Pod does not carry.
    The Pod *does* have ``DEILE_BOT_ENDPOINT`` + ``DEILE_BOT_AUTH_TOKEN``, so we
    send DMs through the bot's control plane (the same facade the worker uses
    for status messages). When ``user_id`` is empty the notifier is disabled
    and the dm_fn is never invoked.
    """
    from deile.orchestration.pipeline.notifier import DiscordNotifier

    async def _dm(uid: str, text: str):
        from deile.integrations.bot import get_bot_client
        return await get_bot_client().dm_send(user_id=uid, text=text)

    return DiscordNotifier(user_id or None, dm_fn=_dm)


async def run_pipeline_forever() -> int:
    """Build the monitor from settings, start it, and block until SIGTERM/SIGINT."""
    from deile.orchestration.pipeline.monitor import (
        PipelineMonitor, build_default_pipeline_config)

    cfg = build_default_pipeline_config()
    # review_callback stays None on purpose: the optional issue-body summary
    # would require a local LLM agent. The pipeline still flows
    # nova → revisada → implement without it; the worker does the real work.
    monitor = PipelineMonitor(cfg, notifier=_build_notifier(cfg.notify_user_id or ""))
    logger.info(
        "starting pipeline monitor (repo=%s, dispatch=%s, poll=%ss, identity=%s)",
        cfg.repo, cfg.dispatch_mode, cfg.poll_interval_seconds,
        monitor.identity.monitor_id,
    )
    await monitor.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # pragma: no cover — non-unix
            pass
    try:
        await stop.wait()
    finally:
        logger.info("stopping pipeline monitor")
        await monitor.stop()
    return 0


def main() -> int:
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return asyncio.run(run_pipeline_forever())


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
