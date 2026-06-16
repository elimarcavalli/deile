"""Plugin Sandbox - skeleton sem isolamento real.

Esta classe NÃO isola plugins. `isolate_plugin` apenas guarda a instância
em um dicionário; `execute_in_sandbox` faz dispatch direto via `getattr`,
sem qualquer contenção (subprocess, container, RestrictedPython, etc.).

`PluginManager` também não invoca esta classe — plugins carregados rodam
no processo DEILE com privilégios totais. Trate como código auditável e
só carregue plugins de fontes confiáveis.

Referência: issue #54.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


class PluginSandbox:
    """Skeleton de sandbox de plugins. NÃO fornece isolamento.

    Mantida como ponto de extensão futuro. Hoje todo método é equivalente
    a invocar o plugin diretamente — não confie nesta classe para garantir
    segurança. Veja issue #54 para histórico e roadmap.
    """

    def __init__(self):
        self._isolated_plugins: Dict[str, Any] = {}

    async def isolate_plugin(self, plugin_id: str, plugin_instance: Any) -> bool:
        """Registra o plugin no dicionário interno (não isola)."""
        try:
            self._isolated_plugins[plugin_id] = plugin_instance
            logger.debug(
                "Plugin %s registrado no PluginSandbox skeleton (sem isolamento real)",
                plugin_id,
            )
            return True

        except Exception as e:
            logger.error(f"Erro ao registrar plugin {plugin_id}: {e}")
            return False

    async def execute_in_sandbox(
        self, plugin_id: str, method: str, *args, **kwargs
    ) -> Any:
        """Despacha método do plugin diretamente (sem contenção)."""
        if plugin_id not in self._isolated_plugins:
            raise Exception(f"Plugin {plugin_id} não está registrado")

        plugin = self._isolated_plugins[plugin_id]

        if not hasattr(plugin, method):
            raise Exception(f"Método {method} não encontrado no plugin {plugin_id}")

        return await getattr(plugin, method)(*args, **kwargs)
