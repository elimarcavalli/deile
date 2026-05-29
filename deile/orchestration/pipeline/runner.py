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

Throughput: implement dispatches are fire-and-forget (``wait_for_result=False``
in the payload — issue #381 fix); the pipeline reconciles ground truth (GitHub
labels / PR existence) on the next tick instead of blocking. Review/refine/
follow_ups dispatches are still synchronous because the stage handler needs the
structured result. ``tick()`` itself is sequential — a single implement dispatch
no longer stalls the whole loop, but other stages within the same tick still
run after it returns (immediately, since the 202 is received in milliseconds).
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import os
import signal
import sys
from pathlib import Path

logger = logging.getLogger("deile.pipeline.runner")


def _try_import_pipeline_status_server():
    """Carrega ``pipeline_status_server.py`` se presente em ``/app/`` ou no
    ``infra/k8s/`` do worktree. Retorna o módulo ou ``None`` (silenciosamente
    sem o server — runner continua sem painel introspection).

    O server vive em ``infra/k8s/``, fora do pacote ``deile`` (paridade com
    ``claude_worker_server`` e ``worker_server``). No pod ele é copiado pra
    ``/app/pipeline_status_server.py`` pelo Dockerfile. Em dev local ele
    pode estar em ``infra/k8s/`` — testamos ambos.
    """
    candidates = [
        Path("/app/pipeline_status_server.py"),
        Path(__file__).resolve().parents[3] / "infra" / "k8s" / "pipeline_status_server.py",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                "pipeline_status_server", str(path),
            )
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules["pipeline_status_server"] = mod
            spec.loader.exec_module(mod)
            return mod
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to load pipeline_status_server from %s: %s — "
                "introspection endpoints disabled this run", path, exc,
            )
            return None
    logger.info(
        "pipeline_status_server.py not present in /app/ or infra/k8s/ — "
        "introspection endpoints disabled this run",
    )
    return None


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


async def _start_status_server(monitor) -> "tuple|None":
    """Sobe o ``pipeline_status_server`` como ``aiohttp.AppRunner`` no MESMO
    event loop do monitor. Pipeline + server compartilham loop → server
    consulta state sem locks. Retorna ``(runner, site)`` pra cleanup, ou
    None se o módulo não está disponível (server-less mode).

    Bearer token vem de ``DEILE_PIPELINE_STATUS_AUTH_TOKEN`` env OU
    ``/run/secrets/pipeline-status/AUTH_TOKEN`` (criado por novo manifest).

    Port default 8768 (manifest 46 expõe). Set via
    ``DEILE_PIPELINE_STATUS_PORT`` env pra testes/dev.
    """
    mod = _try_import_pipeline_status_server()
    if mod is None:
        return None
    try:
        from aiohttp import web
    except ImportError:
        logger.warning("aiohttp ausente — pipeline_status_server desabilitado")
        return None

    # Wire o singleton state global. Monitor publica via record_*; server lê.
    try:
        state = mod.get_global_state()
        # Conecta o force-tick callback. Wraps em coroutine schedule pra não
        # bloquear o handler HTTP — o tick rola no loop principal do monitor.
        if hasattr(state, "set_force_tick_callback"):
            def _force_tick_cb():
                asyncio.ensure_future(monitor.tick())
            state.set_force_tick_callback(_force_tick_cb)
        # Injeta o state no monitor pra ele publicar em cada tick.
        try:
            monitor._status_state = state  # noqa: SLF001
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("falha ao wirear state global do status server: %s", exc)

    host = os.environ.get("DEILE_PIPELINE_STATUS_HOST", "0.0.0.0")
    port = int(os.environ.get("DEILE_PIPELINE_STATUS_PORT", "8768"))
    try:
        app = mod.build_app()
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline_status_server build_app failed: %s", exc)
        return None
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    try:
        await site.start()
        logger.info(
            "pipeline_status_server listening on %s:%d (issue #347 introspection)",
            host, port,
        )
    except OSError as exc:
        logger.warning("pipeline_status_server bind %s:%d failed: %s",
                       host, port, exc)
        await runner.cleanup()
        return None
    return (runner, site)


def _warn_if_no_forge_token() -> None:
    """Emit a WARNING if neither GITHUB_TOKEN nor GITLAB_TOKEN is present."""
    has_github = bool(os.environ.get("GITHUB_TOKEN"))
    has_gitlab = bool(os.environ.get("GITLAB_TOKEN") or os.environ.get("GL_TOKEN"))
    if not has_github and not has_gitlab:
        logger.warning(
            "no forge token found — set GITHUB_TOKEN or GITLAB_TOKEN "
            "before starting the pipeline, otherwise forge API calls will fail"
        )


async def run_pipeline_forever() -> int:
    """Build the monitor from settings, start it, and block until SIGTERM/SIGINT."""
    from deile.orchestration.pipeline.monitor import (
        PipelineMonitor, build_default_pipeline_config)

    _warn_if_no_forge_token()
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
    # Sobe o HTTP introspection server ANTES de monitor.start() — o catch-up
    # pode bloquear por minutos (N ticks × M dispatches) e o readiness probe
    # (:8768/v1/health) ficaria falhando até o catch-up terminar (issue #381).
    # O monitor precisa existir (construído acima) mas NÃO precisa estar
    # started para o server subir — o force-tick callback só precisa de
    # monitor.tick() que é disponível antes de start(). Best-effort: se o
    # módulo não está presente OU bind falha, pipeline continua funcional.
    status_server = await _start_status_server(monitor)

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
        if status_server is not None:
            runner, _site = status_server
            try:
                await runner.cleanup()
            except Exception as exc:  # noqa: BLE001
                logger.warning("status_server cleanup failed: %s", exc)
        logger.info("stopping pipeline monitor")
        await monitor.stop()
    return 0


def main() -> int:
    from deile.log_mgmt import init_logging
    init_logging(pod_name="deile-pipeline")
    return asyncio.run(run_pipeline_forever())


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
