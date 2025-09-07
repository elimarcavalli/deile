"""Classes base para sistema de comandos slash"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from enum import Enum
import asyncio

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text


class CommandStatus(Enum):
    """Status de execução de comando"""
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


@dataclass
class CommandContext:
    """Contexto de execução de comando"""
    user_input: str
    args: str = ""
    session_id: str = "default"
    working_directory: str = "."
    
    # Referências para outros componentes (injetadas dinamicamente)
    agent: Optional[Any] = None
    ui_manager: Optional[Any] = None
    config_manager: Optional[Any] = None
    session: Optional[Any] = None
    
    # Dados adicionais
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def get_session_data(self, key: str, default: Any = None) -> Any:
        """Obtém dados da sessão"""
        if self.session and hasattr(self.session, 'context_data'):
            return self.session.context_data.get(key, default)
        return default
    
    def set_session_data(self, key: str, value: Any) -> None:
        """Define dados na sessão"""
        if self.session and hasattr(self.session, 'context_data'):
            self.session.context_data[key] = value


@dataclass
class CommandResult:
    """Resultado de execução de comando"""
    success: bool
    content: Union[str, Table, Panel, Text, Any] = ""
    content_type: str = "text"  # text, rich, json, error
    status: CommandStatus = CommandStatus.SUCCESS
    metadata: Dict[str, Any] = field(default_factory=dict)
    execution_time: Optional[float] = None
    error: Optional[Exception] = None
    
    def __post_init__(self):
        if not self.success and self.status == CommandStatus.SUCCESS:
            self.status = CommandStatus.ERROR
    
    @classmethod
    def success_result(
        cls, 
        content: Union[str, Any], 
        content_type: str = "text",
        **metadata
    ) -> 'CommandResult':
        """Cria resultado de sucesso"""
        return cls(
            success=True,
            content=content,
            content_type=content_type,
            status=CommandStatus.SUCCESS,
            metadata=metadata
        )
    
    @classmethod
    def error_result(
        cls, 
        message: str, 
        error: Optional[Exception] = None,
        **metadata
    ) -> 'CommandResult':
        """Cria resultado de erro"""
        return cls(
            success=False,
            content=message,
            content_type="error",
            status=CommandStatus.ERROR,
            error=error,
            metadata=metadata
        )


class SlashCommand(ABC):
    """Interface base para comandos slash
    
    Implementa o padrão Strategy para diferentes tipos de comandos.
    """
    
    def __init__(self, command_config=None):
        from ..config.manager import CommandConfig
        
        if command_config is None:
            # Configuração padrão baseada no nome da classe
            command_config = CommandConfig(
                name=self.__class__.__name__.lower().replace('command', ''),
                description="Command description",
                action="default_action"
            )
        
        self.config = command_config
        self._execution_count = 0
        self._last_execution_time = 0
    
    @property
    def name(self) -> str:
        """Nome do comando"""
        return self.config.name
    
    @property
    def description(self) -> str:
        """Descrição do comando"""
        return self.config.description
    
    @property
    def aliases(self) -> List[str]:
        """Aliases do comando"""
        return self.config.aliases
    
    @property
    def enabled(self) -> bool:
        """Verifica se comando está habilitado"""
        return self.config.enabled
    
    @property
    def has_prompt_template(self) -> bool:
        """Verifica se comando tem template para LLM"""
        return self.config.prompt_template is not None
    
    @property
    def execution_count(self) -> int:
        """Contador de execuções"""
        return self._execution_count
    
    @abstractmethod
    async def execute(self, context: CommandContext) -> CommandResult:
        """Executa o comando
        
        Args:
            context: Contexto de execução com args, session, etc.
            
        Returns:
            CommandResult: Resultado da execução
        """
        pass
    
    def get_prompt_for_llm(self, args: str, **kwargs) -> Optional[str]:
        """Retorna prompt processado para LLM ou None para execução direta"""
        if not self.config.prompt_template:
            return None
        
        try:
            return self.config.prompt_template.format(
                args=args,
                **kwargs
            )
        except KeyError as e:
            return f"Error in prompt template: missing variable {e}"
    
    async def can_execute(self, context: CommandContext) -> bool:
        """Verifica se o comando pode ser executado no contexto atual"""
        return self.enabled
    
    async def validate_args(self, args: str) -> List[str]:
        """Valida argumentos do comando
        
        Returns:
            List[str]: Lista de erros de validação (vazia se ok)
        """
        return []
    
    async def get_help(self) -> str:
        """Retorna ajuda detalhada do comando"""
        help_text = f"**/{self.name}** - {self.description}\n\n"
        
        if self.aliases:
            help_text += f"**Aliases:** {', '.join(f'/{alias}' for alias in self.aliases)}\n\n"
        
        if self.has_prompt_template:
            help_text += "Este comando utiliza IA para processar sua solicitação.\n"
        else:
            help_text += "Este comando é executado diretamente pelo sistema.\n"
        
        help_text += f"\n**Execuções:** {self.execution_count}"
        
        return help_text
    
    async def get_suggestions(self, partial_args: str) -> List[str]:
        """Retorna sugestões de autocompletar para argumentos"""
        return []
    
    def _record_execution(self, execution_time: float) -> None:
        """Registra estatísticas de execução"""
        self._execution_count += 1
        self._last_execution_time = execution_time
    
    def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do comando"""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "execution_count": self._execution_count,
            "last_execution_time": self._last_execution_time,
            "has_prompt_template": self.has_prompt_template,
            "aliases": self.aliases
        }
    
    def __str__(self) -> str:
        return f"/{self.name}"
    
    def __repr__(self) -> str:
        return f"<SlashCommand: /{self.name}>"


class DirectCommand(SlashCommand):
    """Base para comandos de execução direta (sem LLM)"""
    
    def __init__(self, command_config=None):
        super().__init__(command_config)
        # Garante que não há prompt template
        if self.config.prompt_template is not None:
            self.config.prompt_template = None


class LLMCommand(SlashCommand):
    """Base para comandos que usam LLM"""
    
    def __init__(self, command_config=None):
        super().__init__(command_config)
        # Garante que há prompt template
        if self.config.prompt_template is None:
            self.config.prompt_template = "Process this request: {args}"
    
    async def execute(self, context: CommandContext) -> CommandResult:
        """Execução padrão para comandos LLM"""
        # Comandos LLM retornam o prompt para ser processado pelo agente
        prompt = self.get_prompt_for_llm(context.args)
        
        return CommandResult.success_result(
            content=prompt,
            content_type="llm_prompt",
            command_name=self.name,
            original_args=context.args
        )