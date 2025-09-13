"""Hot Loader - Recarregamento de plugins em runtime"""

import asyncio
import logging
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger(__name__)


class PluginFileHandler(FileSystemEventHandler):
    """Handler para mudanças em arquivos de plugins"""

    def __init__(self, plugin_manager):
        self.plugin_manager = plugin_manager
        super().__init__()

    def on_modified(self, event):
        """Chamado quando arquivo é modificado"""
        if event.is_directory:
            return

        if event.src_path.endswith(('.py', '.json')):
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
                logger.info(f"Arquivo modificado em plugin {plugin_id}: {event.src_path}")
                asyncio.create_task(self.plugin_manager.reload_plugin(plugin_id))


class HotLoader:
    """Gerencia hot-reload de plugins"""

    def __init__(self, plugin_manager):
        self.plugin_manager = plugin_manager
        self._observer = None
        self._is_active = False

    async def start(self) -> None:
        """Inicia hot-reload"""
        if self._is_active:
            return

        try:
            self._observer = Observer()
            handler = PluginFileHandler(self.plugin_manager)
            self._observer.schedule(
                handler,
                str(self.plugin_manager.plugins_dir),
                recursive=True
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

            logger.info("Hot-reload de plugins desativado")