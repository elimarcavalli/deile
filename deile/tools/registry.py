"""Sistema de Registry para Tools do DEILE com Function Calling support"""

import asyncio
import importlib
import inspect
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from ..core.exceptions import ToolError, ValidationError
from . import schema_export
from .base import SecurityLevel, Tool, ToolContext, ToolResult, ToolSchema
from .schema_validation import validate_function_arguments

logger = logging.getLogger(__name__)


def _run_coro_sync(coro):
    """Run a coroutine to completion from synchronous code.

    Uses ``asyncio.run`` when no loop is active. When invoked from inside
    a running loop (e.g. ``PlanManager._run_tool_with_params``), the
    coroutine is run in a worker thread so it never reenters the live
    loop — ``loop.run_until_complete`` would raise ``RuntimeError`` there.

    Two constraints apply when the worker-thread path is taken, and
    callers must account for both:

    1. Cancellation/timeout does NOT cross into the worker thread. An
       ``asyncio.CancelledError`` or timeout raised against the caller
       (e.g. an ``asyncio.wait_for`` wrapping a ``PlanManager`` step)
       cannot interrupt the blocking ``.result()`` call — the worker
       runs the coroutine to completion regardless. Step-level
       timeout/cancellation therefore does not propagate into the tool.
    2. The coroutine runs on a fresh event loop in a different thread.
       Any tool invoked through this sync bridge must NOT hold or
       capture resources bound to the caller's event loop (e.g.
       ``asyncio.Lock``, connection pools, async clients/sessions);
       such resources will misbehave or raise
       ``RuntimeError: ... attached to a different loop``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class ToolRegistry:
    """Registry central para descoberta e gerenciamento de Tools
    
    Implementa o padrão Registry para permitir registro automático
    e descoberta dinâmica de tools disponíveis no sistema.
    """
    
    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._tools_by_category: Dict[str, List[Tool]] = defaultdict(list)
        self._enabled_tools: Set[str] = set()
        self._tool_aliases: Dict[str, str] = {}
        self._auto_discovery_enabled = True
    
    def register(self, tool: Tool, aliases: Optional[List[str]] = None) -> None:
        """Registra uma tool no registry
        
        Args:
            tool: Instância da tool a ser registrada
            aliases: Lista opcional de aliases para a tool
            
        Raises:
            ValidationError: Se a tool é inválida
            ToolError: Se já existe uma tool com o mesmo nome
        """
        if not isinstance(tool, Tool):
            raise ValidationError(
                f"Expected Tool instance, got {type(tool)}", 
                field_name="tool",
                field_value=type(tool)
            )
        
        tool_name = tool.name
        if tool_name in self._tools:
            raise ToolError(
                f"Tool '{tool_name}' is already registered",
                tool_name=tool_name,
                error_code="TOOL_ALREADY_EXISTS"
            )
        
        # Registra a tool
        self._tools[tool_name] = tool
        self._tools_by_category[tool.category].append(tool)
        
        # Habilita por padrão se a tool está habilitada
        if tool.is_enabled:
            self._enabled_tools.add(tool_name)
        
        # Registra aliases
        if aliases:
            for alias in aliases:
                if alias in self._tool_aliases:
                    logger.warning(f"Alias '{alias}' already exists, overwriting")
                self._tool_aliases[alias] = tool_name
    
    def unregister(self, tool_name: str) -> bool:
        """Remove uma tool do registry
        
        Args:
            tool_name: Nome da tool a ser removida
            
        Returns:
            bool: True se a tool foi removida com sucesso
        """
        if tool_name not in self._tools:
            return False
        
        tool = self._tools[tool_name]
        
        # Remove da categoria
        self._tools_by_category[tool.category].remove(tool)
        
        # Remove dos habilitados
        self._enabled_tools.discard(tool_name)
        
        # Remove aliases
        aliases_to_remove = [
            alias for alias, name in self._tool_aliases.items() 
            if name == tool_name
        ]
        for alias in aliases_to_remove:
            del self._tool_aliases[alias]
        
        # Remove a tool
        del self._tools[tool_name]
        return True
    
    def get(self, tool_name: str) -> Optional[Tool]:
        """Obtém uma tool pelo nome ou alias
        
        Args:
            tool_name: Nome ou alias da tool
            
        Returns:
            Tool: Instância da tool ou None se não encontrada
        """
        # Tenta pelo nome direto
        if tool_name in self._tools:
            return self._tools[tool_name]
        
        # Tenta pelos aliases
        if tool_name in self._tool_aliases:
            real_name = self._tool_aliases[tool_name]
            return self._tools.get(real_name)
        
        return None
    
    def get_enabled(self, tool_name: str) -> Optional[Tool]:
        """Obtém uma tool apenas se ela estiver habilitada
        
        Args:
            tool_name: Nome ou alias da tool
            
        Returns:
            Tool: Instância da tool se habilitada, None caso contrário
        """
        tool = self.get(tool_name)
        if tool and tool.is_enabled and tool_name in self._enabled_tools:
            return tool
        return None
    
    def list_all(self) -> List[Tool]:
        """Lista todas as tools registradas"""
        return list(self._tools.values())
    
    def list_enabled(self) -> List[Tool]:
        """Lista apenas as tools habilitadas"""
        return [
            tool for tool in self._tools.values()
            if tool.is_enabled and tool.name in self._enabled_tools
        ]
    
    def enable_tool(self, tool_name: str) -> bool:
        """Habilita uma tool
        
        Args:
            tool_name: Nome da tool
            
        Returns:
            bool: True se foi habilitada com sucesso
        """
        tool = self.get(tool_name)
        if not tool:
            return False
        
        tool.enable()
        self._enabled_tools.add(tool.name)  # Usa o nome real, não o alias
        return True
    
    def disable_tool(self, tool_name: str) -> bool:
        """Desabilita uma tool
        
        Args:
            tool_name: Nome da tool
            
        Returns:
            bool: True se foi desabilitada com sucesso
        """
        tool = self.get(tool_name)
        if not tool:
            return False
        
        tool.disable()
        self._enabled_tools.discard(tool.name)  # Usa o nome real, não o alias
        return True
    
    async def execute_tool(
        self, 
        tool_name: str, 
        context: ToolContext
    ) -> ToolResult:
        """Executa uma tool específica
        
        Args:
            tool_name: Nome da tool
            context: Contexto de execução
            
        Returns:
            ToolResult: Resultado da execução
            
        Raises:
            ToolError: Se a tool não existe ou não está habilitada
        """
        tool = self.get_enabled(tool_name)
        if not tool:
            raise ToolError(
                f"Tool '{tool_name}' not found or not enabled",
                tool_name=tool_name,
                error_code="TOOL_NOT_AVAILABLE"
            )
        
        try:
            # Valida contexto
            if not await tool.validate_context(context):
                raise ToolError(
                    f"Invalid context for tool '{tool_name}'",
                    tool_name=tool_name,
                    error_code="INVALID_CONTEXT"
                )
            
            # Executa a tool
            return await tool.execute(context)
            
        except Exception as e:
            if isinstance(e, ToolError):
                raise
            
            raise ToolError(
                f"Error executing tool '{tool_name}': {str(e)}",
                tool_name=tool_name,
                error_code="EXECUTION_ERROR"
            ) from e
    
    def auto_discover(self, package_names: Optional[List[str]] = None) -> int:
        """Descobre automaticamente tools em pacotes
        
        Args:
            package_names: Lista de pacotes para descobrir (opcional)
            
        Returns:
            int: Número de tools descobertas
        """
        if not self._auto_discovery_enabled:
            return 0
        
        if package_names is None:
            package_names = [
                'deile.tools.file_tools',
                'deile.tools.execution_tools',
                'deile.tools.search_tool',
                'deile.tools.bash_tool',
                'deile.tools.vision_tool',
                'deile.tools.pipeline_tool',
                'deile.tools.pipeline_schedule_tool',
                'deile.tools.cron_create_tool',
                'deile.tools.cron_list_tool',
                'deile.tools.cron_delete_tool',
                'deile.tools.worktree_tool',
                'deile.tools.dispatch_deile_task',
            ]

        discovered_count = 0

        for package_name in package_names:
            try:
                discovered_count += self._discover_in_package(package_name)
            except Exception as e:
                logger.warning(f"Failed to discover tools in {package_name}: {e}")

        # Conditional registration of messaging tools (`messaging.discord_*`).
        # The dedicated module decides whether to register based on
        # `deilebot` availability AND env configuration.
        try:
            from .messaging.auto_discover import register_messaging_tools

            discovered_count += register_messaging_tools(self)
        except Exception as e:  # pragma: no cover
            logger.warning(f"messaging tool registration failed: {e}")

        return discovered_count
    
    def _discover_in_package(self, package_name: str) -> int:
        """Descobre tools em um pacote específico"""
        try:
            module = importlib.import_module(package_name)
        except ImportError:
            logger.debug(f"Package {package_name} not found for auto-discovery")
            return 0
        
        discovered_count = 0
        
        # Procura por classes que herdam de Tool
        for name in dir(module):
            obj = getattr(module, name)
            if (
                inspect.isclass(obj) and 
                issubclass(obj, Tool) and 
                obj != Tool and
                not inspect.isabstract(obj)
            ):
                try:
                    # Instancia e registra a tool
                    tool_instance = obj()
                    if tool_instance.name not in self._tools:
                        self.register(tool_instance)
                        discovered_count += 1
                except Exception as e:
                    logger.warning(f"Failed to register discovered tool {name}: {e}")
        
        return discovered_count
    
    def get_gemini_functions(self, authorized_only: bool = True, security_level: Optional[SecurityLevel] = None) -> List:
        """Retorna tools no formato FunctionDeclaration para o Google GenAI SDK."""
        return schema_export.get_gemini_functions(
            self._tools, self._enabled_tools, authorized_only, security_level
        )

    def get_anthropic_tools(
        self,
        authorized_only: bool = True,
        security_level: Optional[SecurityLevel] = None,
    ) -> List[Dict]:
        """Return tools in Anthropic tool_use format."""
        return schema_export.get_anthropic_tools(
            self._tools, self._enabled_tools, authorized_only, security_level
        )

    def get_openai_functions(
        self,
        authorized_only: bool = True,
        security_level: Optional[SecurityLevel] = None,
    ) -> List[Dict]:
        """Return tools in OpenAI / DeepSeek function_call format."""
        return schema_export.get_openai_functions(
            self._tools, self._enabled_tools, authorized_only, security_level
        )

    def load_schemas_from_directory(self, schemas_dir: Path) -> int:
        """Carrega schemas de tools de um diretório
        
        Args:
            schemas_dir: Diretório contendo arquivos JSON de schemas
            
        Returns:
            int: Número de schemas carregados
        """
        if not schemas_dir.exists():
            logger.warning(f"Schemas directory not found: {schemas_dir}")
            return 0
        
        loaded_count = 0
        
        for schema_file in schemas_dir.glob("*.json"):
            try:
                schema = ToolSchema.from_json_file(schema_file)
                
                # Associa schema à tool se ela existir
                if schema.name in self._tools:
                    tool = self._tools[schema.name]
                    tool.set_schema(schema)
                    loaded_count += 1
                    logger.debug(f"Loaded schema for tool: {schema.name}")
                else:
                    logger.warning(f"Schema found for unregistered tool: {schema.name}")
                    
            except Exception as e:
                logger.error(f"Failed to load schema from {schema_file}: {e}")
        
        logger.info(f"Loaded {loaded_count} tool schemas from {schemas_dir}")
        return loaded_count
    
    def execute_function_call(
        self, 
        function_name: str, 
        arguments: Dict[str, Any],
        execution_context: Optional[Dict[str, Any]] = None
    ) -> ToolResult:
        """Executa uma function call da Gemini API
        
        Args:
            function_name: Nome da função a ser executada
            arguments: Argumentos da função
            execution_context: Contexto de execução adicional
            
        Returns:
            ToolResult: Resultado da execução
        """
        # Resolve nome da tool (pode ser alias)
        tool_name = self._tool_aliases.get(function_name, function_name)
        
        if tool_name not in self._tools:
            return ToolResult.error_result(
                f"Function '{function_name}' not found",
                error_code="FUNCTION_NOT_FOUND"
            )
        
        tool = self._tools[tool_name]
        
        # Verifica se tool está habilitada
        if tool_name not in self._enabled_tools:
            return ToolResult.error_result(
                f"Function '{function_name}' is disabled",
                error_code="FUNCTION_DISABLED"
            )
        
        # Valida argumentos se schema disponível
        if tool.schema:
            validation_result = validate_function_arguments(tool.schema, arguments)
            if not validation_result["valid"]:
                return ToolResult.error_result(
                    f"Invalid arguments for '{function_name}': {validation_result['errors']}",
                    error_code="INVALID_ARGUMENTS"
                )
        
        # Cria contexto de execução
        context = ToolContext(
            user_input="",  # Function calls não têm user_input direto
            parsed_args=arguments,
            session_data=execution_context or {},
            working_directory=execution_context.get("working_directory", ".") if execution_context else ".",
            metadata={
                "execution_method": "function_call",
                "function_name": function_name,
                "tool_name": tool_name
            }
        )
        
        # Executa tool de forma síncrona (Function Calling é síncrono na API)
        try:
            # Se é SyncTool, executa diretamente
            if hasattr(tool, 'execute_sync'):
                return tool.execute_sync(context)
            # Executa async tool de forma síncrona — seguro tanto fora
            # quanto dentro de um event loop ativo.
            return _run_coro_sync(tool.execute(context))

        except Exception as e:
            logger.error(f"Error executing function call '{function_name}': {e}")
            return ToolResult.error_result(
                f"Execution error: {str(e)}",
                error=e,
                error_code="EXECUTION_ERROR"
            )
    
    def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do registry"""
        total_tools = len(self._tools)
        enabled_tools = len(self._enabled_tools)
        categories = len(self._tools_by_category)
        
        category_stats = {
            category: len(tools) 
            for category, tools in self._tools_by_category.items()
        }
        
        # Estatísticas de Function Calling
        tools_with_schemas = sum(1 for tool in self._tools.values() if tool.schema is not None)
        function_definitions = sum(
            1 for name, tool in self._tools.items()
            if name in self._enabled_tools and tool.get_function_definition() is not None
        )
        
        return {
            "total_tools": total_tools,
            "enabled_tools": enabled_tools,
            "disabled_tools": total_tools - enabled_tools,
            "categories": categories,
            "category_breakdown": category_stats,
            "total_aliases": len(self._tool_aliases),
            "auto_discovery_enabled": self._auto_discovery_enabled,
            "tools_with_schemas": tools_with_schemas,
            "available_functions": function_definitions
        }
    
    def clear(self) -> None:
        """Limpa todos os tools registrados"""
        self._tools.clear()
        self._tools_by_category.clear()
        self._enabled_tools.clear()
        self._tool_aliases.clear()
    
    def disable_auto_discovery(self) -> None:
        """Desabilita descoberta automática"""
        self._auto_discovery_enabled = False
    
    def enable_auto_discovery(self) -> None:
        """Habilita descoberta automática"""
        self._auto_discovery_enabled = True
    
    def __len__(self) -> int:
        return len(self._tools)
    
    def __contains__(self, tool_name: str) -> bool:
        return tool_name in self._tools or tool_name in self._tool_aliases
    
    def __iter__(self):
        return iter(self._tools.values())


# Singleton instance
_tool_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """Retorna a instância singleton do ToolRegistry com auto-discovery"""
    global _tool_registry
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
        
        # Auto-discover tools
        _tool_registry.auto_discover()
        
        # Carrega schemas se diretório existir
        try:
            schemas_dir = Path(__file__).parent / "schemas"
            if schemas_dir.exists():
                _tool_registry.load_schemas_from_directory(schemas_dir)
                logger.info("Tool schemas loaded successfully")
        except Exception as e:
            logger.warning(f"Failed to load tool schemas: {e}")
    
    return _tool_registry


def register_tool(tool: Tool, aliases: Optional[List[str]] = None) -> None:
    """Função helper para registrar uma tool"""
    registry = get_tool_registry()
    registry.register(tool, aliases)