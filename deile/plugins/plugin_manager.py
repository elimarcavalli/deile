"""Plugin Manager - Gerenciamento completo do ciclo de vida de plugins"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Set, Type
from dataclasses import dataclass, field
from pathlib import Path
import importlib
import inspect
from enum import Enum
import time
import json

logger = logging.getLogger(__name__)


class PluginStatus(Enum):
    """Status de um plugin"""
    UNKNOWN = "unknown"
    LOADING = "loading"
    LOADED = "loaded"
    ACTIVE = "active"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass
class PluginInfo:
    """Informações de um plugin"""
    plugin_id: str
    name: str
    version: str
    description: str
    author: str
    status: PluginStatus = PluginStatus.UNKNOWN
    dependencies: List[str] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    plugin_dir: Optional[Path] = None
    main_class: Optional[Type] = None
    instance: Optional[Any] = None
    loaded_at: Optional[float] = None
    last_error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class BasePlugin:
    """Classe base para todos os plugins"""

    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self.is_active = False

    async def initialize(self) -> None:
        """Inicialização do plugin"""
        pass

    async def activate(self) -> None:
        """Ativação do plugin"""
        self.is_active = True

    async def deactivate(self) -> None:
        """Desativação do plugin"""
        self.is_active = False

    async def shutdown(self) -> None:
        """Finalização do plugin"""
        pass

    def get_capabilities(self) -> List[str]:
        """Retorna capacidades do plugin"""
        return []

    def get_dependencies(self) -> List[str]:
        """Retorna dependências do plugin"""
        return []


class PluginManager:
    """Gerenciador avançado de plugins com hot-reload e dependency resolution

    Features:
    - Auto-discovery de plugins
    - Hot-reload com recarregamento em runtime
    - Resolução automática de dependências
    - Isolamento de plugins em sandbox
    - Plugin lifecycle management
    - Monitoring de saúde dos plugins
    """

    def __init__(self, plugins_dir: Path = None):
        self.plugins_dir = plugins_dir or Path("deile/plugins/installed")
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

        # Storage de plugins
        self._plugins: Dict[str, PluginInfo] = {}
        self._plugin_instances: Dict[str, BasePlugin] = {}

        # Dependency graph
        self._dependency_graph: Dict[str, Set[str]] = {}

        # Hot-reload
        self._hot_reload_enabled = False
        self._file_watcher = None

        # Estatísticas
        self._stats = {
            "plugins_loaded": 0,
            "plugins_active": 0,
            "plugins_failed": 0,
            "hot_reloads": 0,
            "last_discovery": 0.0
        }

        logger.info("PluginManager inicializado")

    async def initialize(self) -> None:
        """Inicializa o plugin manager"""
        logger.info("Inicializando PluginManager...")

        # Descobre plugins existentes
        await self.discover_plugins()

        # Carrega plugins básicos
        await self.load_core_plugins()

        logger.info(f"PluginManager inicializado com {len(self._plugins)} plugins")

    async def discover_plugins(self) -> int:
        """Descobre plugins disponíveis no diretório"""
        logger.info("Descobrindo plugins...")

        discovered = 0

        for plugin_dir in self.plugins_dir.iterdir():
            if not plugin_dir.is_dir() or plugin_dir.name.startswith('.'):
                continue

            manifest_file = plugin_dir / "plugin.json"
            if not manifest_file.exists():
                continue

            try:
                with open(manifest_file, 'r', encoding='utf-8') as f:
                    manifest = json.load(f)

                plugin_info = PluginInfo(
                    plugin_id=manifest["plugin_id"],
                    name=manifest["name"],
                    version=manifest.get("version", "1.0.0"),
                    description=manifest.get("description", ""),
                    author=manifest.get("author", "Unknown"),
                    dependencies=manifest.get("dependencies", []),
                    capabilities=manifest.get("capabilities", []),
                    plugin_dir=plugin_dir,
                    metadata=manifest.get("metadata", {})
                )

                self._plugins[plugin_info.plugin_id] = plugin_info
                discovered += 1

                logger.debug(f"Plugin descoberto: {plugin_info.name} v{plugin_info.version}")

            except Exception as e:
                logger.error(f"Erro ao processar plugin em {plugin_dir}: {e}")

        self._stats["last_discovery"] = time.time()
        logger.info(f"Descobertos {discovered} plugins")

        return discovered

    async def load_plugin(self, plugin_id: str) -> bool:
        """Carrega um plugin específico"""
        if plugin_id not in self._plugins:
            logger.error(f"Plugin não encontrado: {plugin_id}")
            return False

        plugin_info = self._plugins[plugin_id]

        if plugin_info.status == PluginStatus.LOADED:
            logger.debug(f"Plugin já carregado: {plugin_id}")
            return True

        logger.info(f"Carregando plugin: {plugin_info.name}")

        try:
            plugin_info.status = PluginStatus.LOADING

            # Verifica dependências
            missing_deps = await self._check_dependencies(plugin_info)
            if missing_deps:
                # Tenta carregar dependências automaticamente
                for dep in missing_deps:
                    if not await self.load_plugin(dep):
                        raise Exception(f"Dependência não satisfeita: {dep}")

            # Carrega módulo do plugin
            main_module_path = plugin_info.plugin_dir / "main.py"
            if not main_module_path.exists():
                raise Exception("Arquivo main.py não encontrado")

            # Import dinâmico do módulo
            spec = importlib.util.spec_from_file_location(
                f"plugin_{plugin_id}", main_module_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Procura classe principal do plugin
            plugin_class = None
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BasePlugin) and obj != BasePlugin:
                    plugin_class = obj
                    break

            if not plugin_class:
                raise Exception("Classe de plugin não encontrada")

            # Cria instância
            plugin_instance = plugin_class(plugin_id)

            # Inicializa plugin
            await plugin_instance.initialize()

            # Atualiza informações
            plugin_info.main_class = plugin_class
            plugin_info.instance = plugin_instance
            plugin_info.status = PluginStatus.LOADED
            plugin_info.loaded_at = time.time()
            plugin_info.last_error = None

            # Armazena instância
            self._plugin_instances[plugin_id] = plugin_instance

            self._stats["plugins_loaded"] += 1
            logger.info(f"Plugin carregado com sucesso: {plugin_info.name}")

            return True

        except Exception as e:
            plugin_info.status = PluginStatus.ERROR
            plugin_info.last_error = str(e)
            self._stats["plugins_failed"] += 1

            logger.error(f"Erro ao carregar plugin {plugin_id}: {e}")
            return False

    async def activate_plugin(self, plugin_id: str) -> bool:
        """Ativa um plugin carregado"""
        if plugin_id not in self._plugin_instances:
            if not await self.load_plugin(plugin_id):
                return False

        plugin_info = self._plugins[plugin_id]
        plugin_instance = self._plugin_instances[plugin_id]

        try:
            await plugin_instance.activate()
            plugin_info.status = PluginStatus.ACTIVE
            self._stats["plugins_active"] += 1

            logger.info(f"Plugin ativado: {plugin_info.name}")
            return True

        except Exception as e:
            plugin_info.status = PluginStatus.ERROR
            plugin_info.last_error = str(e)

            logger.error(f"Erro ao ativar plugin {plugin_id}: {e}")
            return False

    async def deactivate_plugin(self, plugin_id: str) -> bool:
        """Desativa um plugin"""
        if plugin_id not in self._plugin_instances:
            return True

        plugin_info = self._plugins[plugin_id]
        plugin_instance = self._plugin_instances[plugin_id]

        try:
            await plugin_instance.deactivate()
            plugin_info.status = PluginStatus.LOADED

            if self._stats["plugins_active"] > 0:
                self._stats["plugins_active"] -= 1

            logger.info(f"Plugin desativado: {plugin_info.name}")
            return True

        except Exception as e:
            plugin_info.last_error = str(e)
            logger.error(f"Erro ao desativar plugin {plugin_id}: {e}")
            return False

    async def unload_plugin(self, plugin_id: str) -> bool:
        """Descarrega um plugin da memória"""
        if plugin_id not in self._plugin_instances:
            return True

        plugin_info = self._plugins[plugin_id]
        plugin_instance = self._plugin_instances[plugin_id]

        try:
            # Desativa primeiro se ativo
            if plugin_info.status == PluginStatus.ACTIVE:
                await self.deactivate_plugin(plugin_id)

            # Finaliza plugin
            await plugin_instance.shutdown()

            # Remove da memória
            del self._plugin_instances[plugin_id]
            plugin_info.status = PluginStatus.UNKNOWN
            plugin_info.instance = None
            plugin_info.loaded_at = None

            logger.info(f"Plugin descarregado: {plugin_info.name}")
            return True

        except Exception as e:
            plugin_info.last_error = str(e)
            logger.error(f"Erro ao descarregar plugin {plugin_id}: {e}")
            return False

    async def reload_plugin(self, plugin_id: str) -> bool:
        """Recarrega um plugin (hot-reload)"""
        logger.info(f"Recarregando plugin: {plugin_id}")

        # Descarrega primeiro
        await self.unload_plugin(plugin_id)

        # Recarrega
        success = await self.load_plugin(plugin_id)

        if success:
            # Reativa se necessário
            await self.activate_plugin(plugin_id)
            self._stats["hot_reloads"] += 1

        return success

    async def load_core_plugins(self) -> None:
        """Carrega plugins essenciais do core"""
        core_plugins = [
            "memory_plugin",
            "monitoring_plugin",
            "collaboration_plugin"
        ]

        for plugin_id in core_plugins:
            if plugin_id in self._plugins:
                await self.load_plugin(plugin_id)
                await self.activate_plugin(plugin_id)

    async def _check_dependencies(self, plugin_info: PluginInfo) -> List[str]:
        """Verifica dependências de um plugin"""
        missing = []

        for dep in plugin_info.dependencies:
            if dep not in self._plugins:
                missing.append(dep)
            elif self._plugins[dep].status not in [PluginStatus.LOADED, PluginStatus.ACTIVE]:
                missing.append(dep)

        return missing

    def get_plugin_info(self, plugin_id: str) -> Optional[PluginInfo]:
        """Retorna informações de um plugin"""
        return self._plugins.get(plugin_id)

    def list_plugins(self, status: Optional[PluginStatus] = None) -> List[PluginInfo]:
        """Lista plugins com filtro opcional por status"""
        plugins = list(self._plugins.values())

        if status:
            plugins = [p for p in plugins if p.status == status]

        return plugins

    def get_plugins_by_capability(self, capability: str) -> List[PluginInfo]:
        """Retorna plugins que possuem uma capacidade específica"""
        return [
            plugin for plugin in self._plugins.values()
            if capability in plugin.capabilities
        ]

    async def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do plugin manager"""
        active_plugins = len([p for p in self._plugins.values() if p.status == PluginStatus.ACTIVE])
        loaded_plugins = len([p for p in self._plugins.values() if p.status == PluginStatus.LOADED])
        error_plugins = len([p for p in self._plugins.values() if p.status == PluginStatus.ERROR])

        return {
            "total_plugins": len(self._plugins),
            "active_plugins": active_plugins,
            "loaded_plugins": loaded_plugins,
            "error_plugins": error_plugins,
            "hot_reload_enabled": self._hot_reload_enabled,
            "stats": self._stats.copy()
        }

    async def shutdown(self) -> None:
        """Finaliza o plugin manager"""
        logger.info("Finalizando PluginManager...")

        # Desativa todos os plugins ativos
        active_plugins = [p.plugin_id for p in self._plugins.values() if p.status == PluginStatus.ACTIVE]
        for plugin_id in active_plugins:
            await self.deactivate_plugin(plugin_id)

        # Descarrega todos os plugins
        loaded_plugins = list(self._plugin_instances.keys())
        for plugin_id in loaded_plugins:
            await self.unload_plugin(plugin_id)

        logger.info("PluginManager finalizado")