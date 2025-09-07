"""Interface base para Tools do DEILE com Function Calling support"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from enum import Enum
from pathlib import Path
import asyncio
import json
import logging


logger = logging.getLogger(__name__)


class ToolStatus(Enum):
    """Status de execução de uma tool"""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


class ToolCategory(Enum):
    """Categorias de tools do sistema"""
    FILE = "file"
    EXECUTION = "execution" 
    SEARCH = "search"
    SYSTEM = "system"
    ANALYSIS = "analysis"
    NETWORK = "network"
    DATABASE = "database"
    OTHER = "other"


class SecurityLevel(Enum):
    """Níveis de segurança para tools"""
    SAFE = "safe"
    MODERATE = "moderate"
    DANGEROUS = "dangerous"


@dataclass
class ToolSchema:
    """Schema completo para Function Calling API"""
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema format
    required: List[str] = field(default_factory=list)
    examples: List[Dict[str, Any]] = field(default_factory=list)
    security_level: SecurityLevel = SecurityLevel.MODERATE
    category: ToolCategory = ToolCategory.OTHER
    enabled: bool = True
    max_execution_time: int = 30
    
    def to_gemini_function(self):
        """Converte para FunctionDeclaration do novo Google GenAI SDK"""
        from google.genai.types import FunctionDeclaration
        
        return FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters=self.parameters
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Converte para dicionário completo"""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "required": self.required,
            "examples": self.examples,
            "security_level": self.security_level.value,
            "category": self.category.value,
            "enabled": self.enabled,
            "max_execution_time": self.max_execution_time
        }
    
    @classmethod
    def from_json_file(cls, schema_file: Path) -> 'ToolSchema':
        """Carrega schema de arquivo JSON"""
        try:
            with open(schema_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            return cls(
                name=data['name'],
                description=data['description'],
                parameters=data['parameters'],
                required=data['parameters'].get('required', []),
                examples=data.get('examples', []),
                security_level=SecurityLevel(data.get('security_level', 'moderate')),
                category=ToolCategory(data.get('category', 'other')),
                enabled=data.get('enabled', True),
                max_execution_time=data.get('max_execution_time', 30)
            )
        except Exception as e:
            logger.error(f"Error loading tool schema from {schema_file}: {e}")
            raise


@dataclass
class ToolContext:
    """Contexto de execução de uma tool"""
    user_input: str
    parsed_args: Dict[str, Any] = field(default_factory=dict)
    session_data: Dict[str, Any] = field(default_factory=dict)
    working_directory: str = "."
    file_list: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get value from session_data with default"""
        return self.session_data.get(key, default)
    
    def set(self, key: str, value: Any) -> None:
        """Set value in session_data"""
        self.session_data[key] = value
    
    def get_context_value(self, key: str, default: Any = None) -> Any:
        """Get value from any context location"""
        # Procura em parsed_args primeiro
        if key in self.parsed_args:
            return self.parsed_args[key]
        # Depois em session_data
        if key in self.session_data:
            return self.session_data[key]
        # Por último em metadata
        if key in self.metadata:
            return self.metadata[key]
        return default


@dataclass
class ToolResult:
    """Resultado da execução de uma tool"""
    status: ToolStatus
    data: Any = None
    message: str = ""
    error: Optional[Exception] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    execution_time: float = 0.0
    
    @property
    def is_success(self) -> bool:
        """Verifica se a execução foi bem-sucedida"""
        return self.status == ToolStatus.SUCCESS
    
    @property
    def is_error(self) -> bool:
        """Verifica se houve erro na execução"""
        return self.status == ToolStatus.ERROR
    
    @staticmethod
    def success_result(data: Any = None, message: str = "", **metadata) -> 'ToolResult':
        """Cria um resultado de sucesso"""
        return ToolResult(
            status=ToolStatus.SUCCESS,
            data=data,
            message=message,
            metadata=metadata
        )
    
    @staticmethod
    def error_result(message: str, error: Optional[Exception] = None, error_code: str = "", **metadata) -> 'ToolResult':
        """Cria um resultado de erro"""
        if error_code:
            metadata['error_code'] = error_code
        return ToolResult(
            status=ToolStatus.ERROR,
            message=message,
            error=error,
            metadata=metadata
        )
    
    def __str__(self) -> str:
        if self.message:
            return f"[{self.status.value}] {self.message}"
        return f"[{self.status.value}]"
    
    def get_rich_display(self, tool_name: str = "", args: Dict[str, Any] = None) -> str:
        """Retorna formatação rica para exibir resultado da tool"""
        return f"● {tool_name}({args or ''})\n  ⎿  {self.message}"


class Tool(ABC):
    """Interface base abstrata para todas as tools do DEILE com Function Calling support
    
    Implementa o padrão Strategy para permitir diferentes implementações
    de ferramentas de forma intercambiável e extensível.
    """
    
    def __init__(self, schema: Optional[ToolSchema] = None):
        self._is_enabled = True
        self._execution_count = 0
        self._schema = schema
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Nome único da tool"""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Descrição da funcionalidade da tool"""
        pass
    
    @property
    @abstractmethod
    def category(self) -> str:
        """Categoria da tool (ex: 'file', 'execution', 'search')"""
        pass
    
    @property
    def version(self) -> str:
        """Versão da tool"""
        return "1.0.0"
    
    @property
    def is_enabled(self) -> bool:
        """Verifica se a tool está habilitada"""
        return self._is_enabled
    
    @property
    def execution_count(self) -> int:
        """Contador de execuções da tool"""
        return self._execution_count
    
    @property
    def schema(self) -> Optional[ToolSchema]:
        """Schema da tool para Function Calling"""
        return self._schema
    
    def set_schema(self, schema: ToolSchema) -> None:
        """Define schema da tool"""
        self._schema = schema
    
    def load_schema_from_file(self, schema_file: Path) -> None:
        """Carrega schema de arquivo JSON"""
        self._schema = ToolSchema.from_json_file(schema_file)
    
    def get_function_definition(self) -> Optional[Dict[str, Any]]:
        """Retorna definição da função para Gemini API"""
        if not self._schema:
            return None
        return self._schema.to_gemini_function()
    
    def enable(self) -> None:
        """Habilita a tool"""
        self._is_enabled = True
    
    def disable(self) -> None:
        """Desabilita a tool"""
        self._is_enabled = False
    
    @abstractmethod
    async def execute(self, context: ToolContext) -> ToolResult:
        """Executa a tool com o contexto fornecido
        
        Args:
            context: Contexto de execução contendo dados necessários
            
        Returns:
            ToolResult: Resultado da execução
            
        Raises:
            ToolError: Erro específico da tool
        """
        pass
    
    async def validate_context(self, context: ToolContext) -> bool:
        """Valida se o contexto é adequado para esta tool
        
        Args:
            context: Contexto a ser validado
            
        Returns:
            bool: True se o contexto é válido
            
        Raises:
            ValidationError: Se o contexto é inválido
        """
        return True
    
    async def can_handle(self, user_input: str) -> bool:
        """Verifica se esta tool pode processar a entrada do usuário
        
        Args:
            user_input: Entrada do usuário
            
        Returns:
            bool: True se a tool pode processar a entrada
        """
        return False
    
    async def get_help(self) -> str:
        """Retorna ajuda sobre como usar a tool"""
        return f"""
Tool: {self.name}
Description: {self.description}
Category: {self.category}
Version: {self.version}
Status: {'Enabled' if self.is_enabled else 'Disabled'}
Executions: {self.execution_count}
"""
    
    def __str__(self) -> str:
        return f"{self.name} ({self.category})"
    
    def __repr__(self) -> str:
        return f"<Tool: {self.name}>"


class AsyncTool(Tool):
    """Tool base para operações assíncronas"""
    
    async def execute_with_timeout(
        self, 
        context: ToolContext, 
        timeout: float = 30.0
    ) -> ToolResult:
        """Executa a tool com timeout
        
        Args:
            context: Contexto de execução
            timeout: Timeout em segundos
            
        Returns:
            ToolResult: Resultado da execução
        """
        try:
            return await asyncio.wait_for(self.execute(context), timeout=timeout)
        except asyncio.TimeoutError:
            return ToolResult(
                status=ToolStatus.ERROR,
                message=f"Tool {self.name} timeout after {timeout}s",
                error=asyncio.TimeoutError(f"Timeout after {timeout}s")
            )


class SyncTool(Tool):
    """Tool base para operações síncronas"""
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Método síncrono que deve ser implementado pelas tools síncronas"""
        raise NotImplementedError("SyncTool must implement execute_sync method")
    
    async def execute(self, context: ToolContext) -> ToolResult:
        """Wrapper assíncrono para tools síncronas"""
        import time
        start_time = time.time()
        
        try:
            self._execution_count += 1
            result = self.execute_sync(context)
            result.execution_time = time.time() - start_time
            return result
        except Exception as e:
            return ToolResult(
                status=ToolStatus.ERROR,
                error=e,
                message=f"Error executing {self.name}: {str(e)}",
                execution_time=time.time() - start_time
            )