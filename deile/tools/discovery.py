"""Auto-descoberta de Tools em pacotes Python e carregamento de schemas.

Extraído de :class:`~deile.tools.registry.ToolRegistry` (SRP): varrer um
pacote em busca de subclasses concretas de ``Tool`` e instanciá-las é uma
responsabilidade distinta do registro/ciclo-de-vida das tools. Idem para
ler schemas de tools a partir de arquivos JSON em disco e associá-los a
tools já registradas. O registry expõe ``auto_discover()`` e
``load_schemas_from_directory()`` que delegam para estas funções, no
mesmo padrão já adotado por ``schema_export.py`` e
``schema_validation.py``.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .base import Tool, ToolSchema

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
    "deile.tools.skill_tools",
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
        if not (
            inspect.isclass(obj)
            and issubclass(obj, Tool)
            and obj is not Tool
            and not inspect.isabstract(obj)
        ):
            continue
        try:
            tool_instance = obj()
            if tool_instance.name not in registry:
                registry.register(tool_instance)
                discovered_count += 1
        except Exception as e:
            logger.warning(
                f"Failed to register discovered tool {name}: {e}",
                exc_info=True,
            )

    return discovered_count


def load_schemas_from_directory(
    registry: "ToolRegistry", schemas_dir: Path
) -> int:
    """Carrega schemas JSON de ``schemas_dir`` e associa-os às tools registradas.

    Schemas cuja tool correspondente não está registrada são logados
    como warning e ignorados. Falhas individuais (JSON malformado,
    schema inválido) são logadas e não interrompem o processamento dos
    demais arquivos.

    Acessa o registry apenas pela API pública (``__contains__``, ``get``)
    — sem tocar em atributos privados.

    Returns:
        número de schemas efetivamente associados a tools nesta chamada.
    """
    if not schemas_dir.exists():
        logger.warning(f"Schemas directory not found: {schemas_dir}")
        return 0

    loaded_count = 0
    for schema_file in schemas_dir.glob("*.json"):
        try:
            schema = ToolSchema.from_json_file(schema_file)
        except Exception as e:
            logger.error(
                f"Failed to load schema from {schema_file}: {e}",
                exc_info=True,
            )
            continue

        if schema.name not in registry:
            logger.warning(
                f"Schema found for unregistered tool: {schema.name}"
            )
            continue

        tool = registry.get(schema.name)
        if tool is not None:
            tool.set_schema(schema)
            loaded_count += 1
            logger.debug(f"Loaded schema for tool: {schema.name}")

    logger.info(f"Loaded {loaded_count} tool schemas from {schemas_dir}")
    return loaded_count
