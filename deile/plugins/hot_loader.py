"""Hot Loader - Recarregamento de plugins em runtime"""

import asyncio
import logging
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)


class PluginFileHandler(FileSystemEventHandler):
    """Handler para mudanças em arquivos de plugins.

    ``watchdog.Observer`` extends ``threading.Thread``, so ``on_modified``
    runs OFF the asyncio loop. Calling ``asyncio.create_task`` from a thread
    with no running loop raises ``RuntimeError`` silently inside watchdog,
    leaving the reload coroutine unscheduled. ``HotLoader.start()`` captures
    the loop and passes it here; we hop back onto it via
    ``run_coroutine_threadsafe``.
    """

    def __init__(self, plugin_manager, loop: asyncio.AbstractEventLoop):
        self.plugin_manager = plugin_manager
        self._loop = loop
        super().__init__()

    def on_modified(self, event):
        """Chamado quando arquivo é modificado"""
        if event.is_directory:
            return

        if event.src_path.endswith((".py", ".json")):
            # Identifica plugin baseado no path
            plugin_path = Path(event.src_path)
            plugin_dir = None

            # Procura diretório do plugin
            for parent in plugin_path.parents:
                if parent.parent == self.plugin_manager.plugins_dir:
                    plugin_dir = parent
                    break

            if plugin_dir:
                plugin_id = plugin_dir.name
                logger.info(
                    f"Arquivo modificado em plugin {plugin_id}: {event.src_path}"
                )
                coro = self.plugin_manager.reload_plugin(plugin_id)
                if self._loop.is_closed():
                    logger.warning(
                        "Hot-reload event for plugin %s ignored: event loop is closed",
                        plugin_id,
                    )
                    coro.close()
                    return
                future = asyncio.run_coroutine_threadsafe(coro, self._loop)
                # Surface failures from the threadsafe future so reload errors
                # are not swallowed silently.
                future.add_done_callback(
                    lambda f, pid=plugin_id: self._on_reload_done(f, pid)
                )

    @staticmethod
    def _on_reload_done(future, plugin_id: str) -> None:
        try:
            future.result()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Hot-reload failed for plugin %s: %s", plugin_id, exc)


class HotLoader:
    """Gerencia hot-reload de plugins"""

    def __init__(self, plugin_manager):
        self.plugin_manager = plugin_manager
        self._observer: Optional[Observer] = None
        self._is_active = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    async def start(self) -> None:
        """Inicia hot-reload"""
        if self._is_active:
            return

        try:
            self._loop = asyncio.get_running_loop()
            self._observer = Observer()
            handler = PluginFileHandler(self.plugin_manager, self._loop)
            self._observer.schedule(
                handler, str(self.plugin_manager.plugins_dir), recursive=True
            )
            self._observer.start()
            self._is_active = True

            logger.info("Hot-reload de plugins ativado")

        except Exception as e:
            logger.error(f"Erro ao iniciar hot-reload: {e}")

    async def stop(self) -> None:
        """Para hot-reload"""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            self._is_active = False
            self._loop = None

            logger.info("Hot-reload de plugins desativado")
