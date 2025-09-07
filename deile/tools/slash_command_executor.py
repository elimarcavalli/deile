"""Tool para execução de comandos slash integrado ao sistema de parsers"""

import logging
from typing import Dict, Any, Optional, List

from .base import Tool, ToolContext, ToolResult, ToolCategory
from ..commands.registry import get_command_registry
from ..commands.base import CommandContext

logger = logging.getLogger(__name__)


class SlashCommandExecutor(Tool):
    """
    Tool responsável por executar comandos slash identificados pelo CommandParser.
    Atua como ponte entre o sistema de parsers e o sistema de comandos.
    """
    
    def __init__(self):
        super().__init__(
            name="slash_command_executor",
            description="Executa comandos slash (/help, /debug, /status, etc.) integrados ao sistema",
            category=ToolCategory.SYSTEM
        )
        self._command_registry = None
    
    @property
    def required_context(self) -> List[str]:
        """Contextos obrigatórios para esta tool"""
        return ["config_manager"]
    
    @property
    def optional_context(self) -> List[str]:
        """Contextos opcionais para esta tool"""
        return ["agent", "ui_manager", "session", "working_directory"]
    
    def validate_context(self, context: ToolContext) -> bool:
        """Valida se o contexto tem as informações necessárias"""
        if not super().validate_context(context):
            return False
        
        # Verifica se tem argumentos com comando
        parsed_args = context.parsed_args
        if not parsed_args or "command_name" not in parsed_args:
            logger.error("SlashCommandExecutor requires command_name in parsed_args")
            return False
        
        return True
    
    async def execute_async(self, context: ToolContext) -> ToolResult:
        """Executa comando slash de forma assíncrona"""
        try:
            # Valida contexto
            if not self.validate_context(context):
                return ToolResult.error_result(
                    "Invalid context for SlashCommandExecutor",
                    error_code="INVALID_CONTEXT"
                )
            
            # Extrai informações do contexto
            config_manager = context.get_context_value("config_manager")
            command_name = context.parsed_args["command_name"]
            raw_args = context.parsed_args.get("raw_args", "")
            
            # Obtém registry de comandos
            if not self._command_registry:
                self._command_registry = get_command_registry(config_manager)
            
            # Verifica se comando existe
            if not self._command_registry.has_command(command_name):
                return ToolResult.error_result(
                    f"Command '/{command_name}' not found in registry",
                    error_code="COMMAND_NOT_FOUND"
                )
            
            # Obtém comando do registry
            command = self._command_registry.get_command(command_name)
            if not command:
                return ToolResult.error_result(
                    f"Failed to retrieve command '/{command_name}'",
                    error_code="COMMAND_RETRIEVAL_FAILED"
                )
            
            # Cria contexto para o comando
            command_context = CommandContext(
                command_name=command_name,
                args=raw_args,
                user_input=context.user_input,
                agent=context.get_context_value("agent"),
                ui_manager=context.get_context_value("ui_manager"),
                config_manager=config_manager,
                session=context.get_context_value("session"),
                working_directory=context.get_context_value("working_directory", ".")
            )
            
            # Executa o comando
            logger.info(f"Executing slash command: /{command_name} with args: {raw_args}")
            command_result = await command.execute(command_context)
            
            # Converte resultado do comando para ToolResult
            if command_result.success:
                return ToolResult.success_result(
                    data=command_result.data,
                    message=f"Command /{command_name} executed successfully",
                    metadata={
                        "command_name": command_name,
                        "command_type": "slash_command",
                        "output_format": command_result.output_format,
                        "execution_time": command_result.execution_time,
                        **command_result.metadata
                    }
                )
            else:
                return ToolResult.error_result(
                    message=command_result.message or f"Command /{command_name} failed",
                    error=command_result.error,
                    error_code="COMMAND_EXECUTION_FAILED",
                    metadata={
                        "command_name": command_name,
                        "command_type": "slash_command",
                        **command_result.metadata
                    }
                )
                
        except Exception as e:
            logger.error(f"Error in SlashCommandExecutor: {e}")
            return ToolResult.error_result(
                message=f"Unexpected error executing slash command: {str(e)}",
                error=e,
                error_code="UNEXPECTED_ERROR"
            )
    
    def execute_sync(self, context: ToolContext) -> ToolResult:
        """Execução síncrona não suportada - comandos slash são assíncronos"""
        return ToolResult.error_result(
            "SlashCommandExecutor only supports async execution",
            error_code="SYNC_NOT_SUPPORTED"
        )
    
    def get_help(self) -> str:
        """Retorna ajuda detalhada sobre esta tool"""
        return """
            SlashCommandExecutor - Executa comandos slash do sistema

            Esta tool é automaticamente invocada pelo CommandParser quando comandos slash
            são detectados na entrada do usuário (ex: /?, /debug, /status).

            Comandos suportados:
            - /help [comando] - Mostra ajuda geral ou específica
            - /debug - Toggle do modo debug
            - /status - Informações do sistema
            - /clear - Limpa sessão e tela
            - /config - Mostra configurações
            - /bash <comando> - Executa comando bash

            A tool atua como ponte entre o sistema de parsers e o sistema de comandos,
            convertendo ParseResults em execuções de comandos reais.
        """
    
    def can_handle_action(self, action: str) -> bool:
        """Verifica se pode tratar uma ação específica"""
        return action == "slash_command_executor" or action.startswith("slash_")