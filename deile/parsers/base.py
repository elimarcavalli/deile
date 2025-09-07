"""Interface base para Parsers do DEILE"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Pattern, Union
from enum import Enum
import re


class ParseStatus(Enum):
    """Status do resultado do parsing"""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    INVALID = "invalid"


@dataclass
class ParsedCommand:
    """Comando parseado da entrada do usuário"""
    action: str
    target: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    flags: List[str] = field(default_factory=list)
    raw_text: str = ""
    
    def has_flag(self, flag: str) -> bool:
        """Verifica se uma flag está presente"""
        return flag in self.flags
    
    def get_arg(self, key: str, default: Any = None) -> Any:
        """Obtém um argumento com valor padrão"""
        return self.arguments.get(key, default)


@dataclass 
class ParseResult:
    """Resultado do parsing de entrada do usuário"""
    status: ParseStatus
    commands: List[ParsedCommand] = field(default_factory=list)
    file_references: List[str] = field(default_factory=list)
    tool_requests: List[str] = field(default_factory=list)
    error_message: str = ""
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_success(self) -> bool:
        """Verifica se o parsing foi bem-sucedido"""
        return self.status == ParseStatus.SUCCESS
    
    @property
    def is_partial(self) -> bool:
        """Verifica se o parsing foi parcial"""
        return self.status == ParseStatus.PARTIAL
    
    @property
    def has_commands(self) -> bool:
        """Verifica se há comandos parseados"""
        return len(self.commands) > 0
    
    @property
    def has_files(self) -> bool:
        """Verifica se há referências de arquivos"""
        return len(self.file_references) > 0
    
    def get_primary_command(self) -> Optional[ParsedCommand]:
        """Retorna o comando principal (primeiro comando)"""
        return self.commands[0] if self.commands else None


class Parser(ABC):
    """Interface base abstrata para parsers de entrada do usuário
    
    Implementa o padrão Strategy para permitir diferentes tipos de parsing
    de forma intercambiável e extensível.
    """
    
    def __init__(self):
        self._is_enabled = True
        self._parse_count = 0
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Nome único do parser"""
        pass
    
    @property
    @abstractmethod
    def description(self) -> str:
        """Descrição da funcionalidade do parser"""
        pass
    
    @property
    @abstractmethod
    def patterns(self) -> List[str]:
        """Padrões regex que este parser reconhece"""
        pass
    
    @property
    def priority(self) -> int:
        """Prioridade do parser (maior = maior prioridade)"""
        return 0
    
    @property
    def version(self) -> str:
        """Versão do parser"""
        return "1.0.0"
    
    @property
    def is_enabled(self) -> bool:
        """Verifica se o parser está habilitado"""
        return self._is_enabled
    
    @property
    def parse_count(self) -> int:
        """Contador de operações de parsing"""
        return self._parse_count
    
    def enable(self) -> None:
        """Habilita o parser"""
        self._is_enabled = True
    
    def disable(self) -> None:
        """Desabilita o parser"""
        self._is_enabled = False
    
    @abstractmethod
    def can_parse(self, input_text: str) -> bool:
        """Verifica se este parser pode processar a entrada
        
        Args:
            input_text: Texto de entrada do usuário
            
        Returns:
            bool: True se o parser pode processar a entrada
        """
        pass
    
    @abstractmethod
    def parse(self, input_text: str) -> ParseResult:
        """Parseia a entrada do usuário
        
        Args:
            input_text: Texto de entrada do usuário
            
        Returns:
            ParseResult: Resultado do parsing
            
        Raises:
            ParserError: Erro específico do parser
        """
        pass
    
    def get_confidence(self, input_text: str) -> float:
        """Calcula a confiança do parser para processar esta entrada
        
        Args:
            input_text: Texto de entrada do usuário
            
        Returns:
            float: Valor entre 0.0 e 1.0 indicando confiança
        """
        if not self.can_parse(input_text):
            return 0.0
        
        # Verifica quantos padrões coincidem
        matches = 0
        for pattern in self.patterns:
            if re.search(pattern, input_text, re.IGNORECASE):
                matches += 1
        
        if not self.patterns:
            return 0.5  # Confiança baixa se não há padrões definidos
        
        return min(1.0, matches / len(self.patterns))
    
    async def parse_async(self, input_text: str) -> ParseResult:
        """Versão assíncrona do parse (para parsers que precisam de I/O)"""
        return self.parse(input_text)
    
    def validate_input(self, input_text: str) -> bool:
        """Valida a entrada antes do parsing
        
        Args:
            input_text: Texto a ser validado
            
        Returns:
            bool: True se a entrada é válida
        """
        return bool(input_text and input_text.strip())
    
    async def get_suggestions(self, partial_input: str) -> List[str]:
        """Retorna sugestões de autocompletar para entrada parcial
        
        Args:
            partial_input: Entrada parcial do usuário
            
        Returns:
            List[str]: Lista de sugestões
        """
        return []
    
    async def get_help(self) -> str:
        """Retorna ajuda sobre como usar este parser"""
        return f"""
Parser: {self.name}
Description: {self.description}
Patterns: {', '.join(self.patterns) if self.patterns else 'None'}
Priority: {self.priority}
Version: {self.version}
Status: {'Enabled' if self.is_enabled else 'Disabled'}
Parse count: {self.parse_count}
"""
    
    def _create_success_result(
        self,
        commands: List[ParsedCommand] = None,
        file_references: List[str] = None,
        tool_requests: List[str] = None,
        confidence: float = 1.0,
        **metadata
    ) -> ParseResult:
        """Helper para criar resultado de sucesso"""
        return ParseResult(
            status=ParseStatus.SUCCESS,
            commands=commands or [],
            file_references=file_references or [],
            tool_requests=tool_requests or [],
            confidence=confidence,
            metadata=metadata
        )
    
    def _create_error_result(
        self,
        error_message: str,
        **metadata
    ) -> ParseResult:
        """Helper para criar resultado de erro"""
        return ParseResult(
            status=ParseStatus.FAILED,
            error_message=error_message,
            metadata=metadata
        )
    
    def __str__(self) -> str:
        return f"{self.name} (priority: {self.priority})"
    
    def __repr__(self) -> str:
        return f"<Parser: {self.name}>"


class RegexParser(Parser):
    """Parser base que usa regex para matching"""
    
    def __init__(self, compiled_patterns: Optional[List[Pattern]] = None):
        super().__init__()
        self._compiled_patterns = compiled_patterns or []
        if not self._compiled_patterns and self.patterns:
            self._compiled_patterns = [
                re.compile(pattern, re.IGNORECASE | re.MULTILINE)
                for pattern in self.patterns
            ]
    
    def can_parse(self, input_text: str) -> bool:
        """Verifica usando padrões regex compilados"""
        if not self._compiled_patterns:
            return False
        
        return any(
            pattern.search(input_text)
            for pattern in self._compiled_patterns
        )
    
    def find_matches(self, input_text: str, pattern_index: int = 0) -> List[re.Match]:
        """Encontra todas as matches para um padrão específico"""
        if pattern_index >= len(self._compiled_patterns):
            return []
        
        return list(self._compiled_patterns[pattern_index].finditer(input_text))


class CompositeParser(Parser):
    """Parser que combina múltiplos parsers"""
    
    def __init__(self, parsers: List[Parser]):
        super().__init__()
        self._parsers = sorted(parsers, key=lambda p: p.priority, reverse=True)
    
    @property
    def patterns(self) -> List[str]:
        """Combina todos os padrões dos parsers filhos"""
        all_patterns = []
        for parser in self._parsers:
            all_patterns.extend(parser.patterns)
        return all_patterns
    
    def can_parse(self, input_text: str) -> bool:
        """Verifica se algum parser filho pode processar"""
        return any(parser.can_parse(input_text) for parser in self._parsers)
    
    def parse(self, input_text: str) -> ParseResult:
        """Tenta parsing com cada parser em ordem de prioridade"""
        self._parse_count += 1
        
        results = []
        for parser in self._parsers:
            if parser.can_parse(input_text):
                try:
                    result = parser.parse(input_text)
                    if result.is_success:
                        return result
                    results.append(result)
                except Exception as e:
                    results.append(ParseResult(
                        status=ParseStatus.FAILED,
                        error_message=f"Parser {parser.name} failed: {str(e)}"
                    ))
        
        # Se nenhum parser conseguiu sucesso completo, retorna o melhor resultado parcial
        partial_results = [r for r in results if r.is_partial]
        if partial_results:
            return max(partial_results, key=lambda r: r.confidence)
        
        return ParseResult(
            status=ParseStatus.FAILED,
            error_message="No parser could handle the input"
        )