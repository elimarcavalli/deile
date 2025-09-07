"""Parser para comandos slash (/comando)"""

import re
from typing import List, Optional

from .base import RegexParser, ParseResult, ParseStatus, ParsedCommand
from ..core.exceptions import ParserError
from ..commands.registry import get_command_registry


class CommandParser(RegexParser):
    """Parser para comandos usando sintaxe /comando"""
    
    def __init__(self, config_manager=None):
        # Padrões para comandos slash
        command_patterns = [
            r'/(\w+)(?:\s+(.+))?',  # /comando ou /comando argumentos
        ]
        
        super().__init__([re.compile(pattern, re.IGNORECASE) for pattern in command_patterns])
        self.config_manager = config_manager
        self._command_registry = None
    
    @property
    def name(self) -> str:
        return "command_parser"
    
    @property
    def description(self) -> str:
        return "Parses slash commands like /help, /list, etc."
    
    @property
    def patterns(self) -> List[str]:
        return [r'/(\w+)(?:\s+(.+))?']
    
    @property
    def priority(self) -> int:
        return 90  # Alta prioridade para comandos específicos
    
    def can_parse(self, input_text: str) -> bool:
        """Verifica se há comandos slash na entrada"""
        if not input_text.strip().startswith('/'):
            return False
            
        # Verifica se é um comando válido no registry
        if self.config_manager and not self._command_registry:
            self._command_registry = get_command_registry(self.config_manager)
            
        if self._command_registry:
            match = self._compiled_patterns[0].search(input_text)
            if match:
                command_name = match.group(1).lower()
                return self._command_registry.has_command(command_name)
        
        return bool(self._compiled_patterns[0].search(input_text))
    
    def parse(self, input_text: str) -> ParseResult:
        """Parseia comandos slash da entrada"""
        self._parse_count += 1
        
        try:
            commands = []
            tool_requests = []
            
            # Obtém registry se ainda não tiver
            if self.config_manager and not self._command_registry:
                self._command_registry = get_command_registry(self.config_manager)
            
            for pattern in self._compiled_patterns:
                match = pattern.search(input_text)
                if match:
                    command_name = match.group(1).lower()
                    arguments = match.group(2) if match.group(2) else ""
                    
                    # Verifica se o comando existe no registry
                    if self._command_registry and not self._command_registry.has_command(command_name):
                        return self._create_error_result(f"Command '/{command_name}' not found")
                    
                    # Cria comando parseado com metadados do registry
                    parsed_command = ParsedCommand(
                        action=f"slash_{command_name}",  # Prefixo para identificar comandos slash
                        target=arguments if arguments else None,
                        arguments={
                            "command_name": command_name,
                            "raw_args": arguments,
                            "use_command_system": True
                        } if arguments else {
                            "command_name": command_name,
                            "use_command_system": True
                        },
                        flags=[],
                        raw_text=input_text
                    )
                    commands.append(parsed_command)
                    
                    # Adiciona o comando slash como tool request
                    tool_requests.append("slash_command_executor")
                    
                    break
            
            if not commands:
                return self._create_error_result("No valid slash commands found")
            
            confidence = 0.95  # Alta confiança para comandos explícitos
            
            return ParseResult(
                status=ParseStatus.SUCCESS,
                commands=commands,
                file_references=[],
                tool_requests=tool_requests,
                confidence=confidence,
                metadata={
                    "parser": self.name,
                    "command_type": "slash_command",
                    "has_command_registry": self._command_registry is not None
                }
            )
            
        except Exception as e:
            return ParseResult(
                status=ParseStatus.FAILED,
                error_message=f"Error parsing slash command: {str(e)}",
                metadata={"parser": self.name, "error": str(e)}
            )
    
    async def get_suggestions(self, partial_input: str) -> List[str]:
        """Retorna sugestões de comandos para autocompletar"""
        suggestions = []
        
        if partial_input.startswith('/'):
            # Usa registry para obter comandos disponíveis
            if self.config_manager and not self._command_registry:
                self._command_registry = get_command_registry(self.config_manager)
            
            if self._command_registry:
                # Obter comandos do registry
                for command in self._command_registry.get_enabled_commands():
                    cmd_name = f"/{command.name}"
                    if cmd_name.startswith(partial_input.lower()):
                        suggestions.append(cmd_name)
                    
                    # Adiciona aliases
                    for alias in getattr(command, 'aliases', []):
                        alias_cmd = f"/{alias}"
                        if alias_cmd.startswith(partial_input.lower()):
                            suggestions.append(alias_cmd)
            else:
                # Fallback para comandos básicos
                available_commands = [
                    "/?", "/debug", "/status", "/cls", "/config",
                    "/bash", "exit"
                ]
                
                for cmd in available_commands:
                    if cmd.startswith(partial_input.lower()):
                        suggestions.append(cmd)
        
        return suggestions
    
    def get_confidence(self, input_text: str) -> float:
        """Calcula confiança baseada na qualidade do comando"""
        if not self.can_parse(input_text):
            return 0.0
        
        # Comandos slash têm alta confiança por serem explícitos
        return 0.95