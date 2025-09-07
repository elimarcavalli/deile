"""Sistema de Registry para Tools do DEILE com Function Calling support"""

from typing import Dict, List, Optional, Type, Set, Any
from collections import defaultdict
from pathlib import Path
import asyncio
import inspect
import importlib
import pkgutil
import logging
import json

from .base import Tool, ToolContext, ToolResult, ToolStatus, ToolSchema, SecurityLevel
from ..core.exceptions import ToolError, ValidationError


logger = logging.getLogger(__name__)


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
        
        # # # logger.info(f"Registered tool: {tool_name} ({tool.category})")
    
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
        
        # (f"Unregistered tool: {tool_name}")
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
    
    def list_by_category(self, category: str) -> List[Tool]:
        """Lista tools por categoria
        
        Args:
            category: Categoria das tools
            
        Returns:
            List[Tool]: Lista de tools da categoria
        """
        return self._tools_by_category.get(category, [])
    
    def get_categories(self) -> List[str]:
        """Lista todas as categorias disponíveis"""
        return list(self._tools_by_category.keys())
    
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
        # (f"Enabled tool: {tool.name}")
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
        # (f"Disabled tool: {tool.name}")
        return True
    
    async def find_suitable_tools(self, user_input: str) -> List[Tool]:
        """Encontra tools adequadas para processar a entrada do usuário
        
        Args:
            user_input: Entrada do usuário
            
        Returns:
            List[Tool]: Lista de tools ordenadas por adequação
        """
        suitable_tools = []
        
        for tool in self.list_enabled():
            try:
                if await tool.can_handle(user_input):
                    suitable_tools.append(tool)
            except Exception as e:
                logger.warning(f"Error checking if tool {tool.name} can handle input: {e}")
        
        # TODO: Implementar ranking por adequação/confiança
        return suitable_tools
    
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
                'deile.tools.slash_command_executor'
            ]
        
        discovered_count = 0
        
        for package_name in package_names:
            try:
                discovered_count += self._discover_in_package(package_name)
            except Exception as e:
                logger.warning(f"Failed to discover tools in {package_name}: {e}")
        
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
        """Retorna tools no formato FunctionDeclaration para novo Google GenAI SDK
        
        Args:
            authorized_only: Se deve retornar apenas tools autorizadas
            security_level: Nível máximo de segurança das tools
            
        Returns:
            List[FunctionDeclaration]: Lista de function declarations para novo SDK
        """
        functions = []
        
        for tool_name, tool in self._tools.items():
            # Verifica se tool está habilitada
            if authorized_only and tool_name not in self._enabled_tools:
                continue
            
            # Verifica nível de segurança
            if security_level and tool.schema:
                if not self._is_security_level_allowed(tool.schema.security_level, security_level):
                    continue
            
            # Obtém definição da função
            function_def = tool.get_function_definition()
            if function_def:
                functions.append(function_def)
        
        logger.debug(f"Generated {len(functions)} function definitions for Gemini API")
        return functions
    
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
            validation_result = self._validate_function_arguments(tool.schema, arguments)
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
            else:
                # Executa async tool de forma síncrona
                loop = asyncio.get_event_loop()
                return loop.run_until_complete(tool.execute(context))
                
        except Exception as e:
            logger.error(f"Error executing function call '{function_name}': {e}")
            return ToolResult.error_result(
                f"Execution error: {str(e)}",
                error=e,
                error_code="EXECUTION_ERROR"
            )
    
    def _is_security_level_allowed(self, tool_level: SecurityLevel, max_level: SecurityLevel) -> bool:
        """Verifica se nível de segurança da tool é permitido"""
        level_hierarchy = {
            SecurityLevel.SAFE: 0,
            SecurityLevel.MODERATE: 1,
            SecurityLevel.DANGEROUS: 2
        }
        return level_hierarchy[tool_level] <= level_hierarchy[max_level]
    
    def _validate_function_arguments(self, schema: ToolSchema, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Valida argumentos de function call contra schema"""
        # Implementação básica de validação
        # TODO: Implementar validação completa usando jsonschema
        
        errors = []
        required_fields = schema.parameters.get("required", [])
        properties = schema.parameters.get("properties", {})
        
        # Verifica campos obrigatórios
        for field in required_fields:
            if field not in arguments:
                errors.append(f"Missing required field: {field}")
        
        # Verifica tipos básicos
        for field, value in arguments.items():
            if field in properties:
                expected_type = properties[field].get("type")
                if expected_type and not self._validate_type(value, expected_type):
                    errors.append(f"Invalid type for field {field}: expected {expected_type}")
        
        return {
            "valid": len(errors) == 0,
            "errors": errors
        }
    
    def _validate_type(self, value: Any, expected_type: str) -> bool:
        """Valida tipo básico de valor"""
        type_mapping = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict
        }
        
        expected_python_type = type_mapping.get(expected_type)
        if expected_python_type:
            return isinstance(value, expected_python_type)
        
        return True  # Se não conhece o tipo, aceita
    
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
        function_definitions = len(self.get_gemini_functions())
        
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
        # ("Cleared all tools from registry")
    
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
            from pathlib import Path
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