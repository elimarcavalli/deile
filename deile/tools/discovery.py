"""Auto-descoberta de Tools em pacotes Python.

Extraído de :class:`~deile.tools.registry.ToolRegistry` (SRP): varrer um
pacote em busca de subclasses concretas de ``Tool`` e instanciá-las é uma
responsabilidade distinta do registro/ciclo-de-vida das tools. O registry
expõe ``auto_discover()`` que delega para estas funções, no mesmo padrão
já adotado por ``schema_export.py`` e ``schema_validation.py``.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from typing import TYPE_CHECKING

from .base import Tool

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = logging.getLogger(__name__)

# Conjunto-padrão de pacotes varridos quando ``auto_discover`` é chamado
# sem argumentos (ver pilar 03 §3 — Registry Pattern / auto-discovery).
DEFAULT_TOOL_PACKAGES = [
    "deile.tools.file_tools",
    "deile.tools.execution_tools",
    "deile.tools.search_tool",
    "deile.tools.bash_tool",
    "deile.tools.vision_tool",
    "deile.tools.pipeline_tool",
    "deile.tools.pipeline_schedule_tool",
    "deile.tools.cron_create_tool",
    "deile.tools.cron_list_tool",
    "deile.tools.cron_delete_tool",
    "deile.tools.worktree_tool",
    "deile.tools.dispatch_deile_task",
]


def discover_tools_in_package(
    registry: "ToolRegistry", package_name: str
) -> int:
    """Importa ``package_name`` e registra toda subclasse concreta de ``Tool``.

    Tools cujo ``name`` já consta no registry são ignoradas. Falhas de
    instanciação/registro de uma tool individual são logadas e não
    interrompem a varredura das demais.

    Returns:
        número de tools efetivamente registradas nesta chamada.
    """
    try:
        module = importlib.import_module(package_name)
    except ImportError:
        logger.debug(f"Package {package_name} not found for auto-discovery")
        return 0

    discovered_count = 0
    for name in dir(module):
        obj = getattr(module, name)
        if (
            inspect.isclass(obj)
            and issubclass(obj, Tool)
            and obj is not Tool
            and not inspect.isabstract(obj)
        ):
            try:
                tool_instance = obj()
                if tool_instance.name not in registry:
                    registry.register(tool_instance)
                    discovered_count += 1
            except Exception as e:
                logger.warning(
                    f"Failed to register discovered tool {name}: {e}"
                )

    return discovered_count
