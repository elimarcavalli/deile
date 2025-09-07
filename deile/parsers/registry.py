"""Sistema de Registry para Parsers do DEILE"""

from typing import Dict, List, Optional, Tuple, Any, Set
import asyncio
import logging
import importlib
import inspect

from .base import Parser, ParseResult, ParseStatus
from ..core.exceptions import ParserError, ValidationError


logger = logging.getLogger(__name__)


class ParserRegistry:
    """Registry central para descoberta e gerenciamento de Parsers
    
    Implementa o padrão Registry para permitir registro automático
    e descoberta dinâmica de parsers disponíveis no sistema.
    """
    
    def __init__(self):
        self._parsers: Dict[str, Parser] = {}
        self._parsers_by_priority: List[Parser] = []
        self._enabled_parsers: Set[str] = set()
        self._parser_aliases: Dict[str, str] = {}
        self._auto_discovery_enabled = True
        self._cache_enabled = True
        self._parse_cache: Dict[str, Tuple[str, ParseResult]] = {}  # input_hash -> (parser_name, result)
    
    def register(self, parser: Parser, aliases: Optional[List[str]] = None) -> None:
        """Registra um parser no registry
        
        Args:
            parser: Instância do parser a ser registrado
            aliases: Lista opcional de aliases para o parser
            
        Raises:
            ValidationError: Se o parser é inválido
            ParserError: Se já existe um parser com o mesmo nome
        """
        if not isinstance(parser, Parser):
            raise ValidationError(
                f"Expected Parser instance, got {type(parser)}",
                field_name="parser", 
                field_value=type(parser)
            )
        
        parser_name = parser.name
        if parser_name in self._parsers:
            raise ParserError(
                f"Parser '{parser_name}' is already registered",
                parser_name=parser_name,
                error_code="PARSER_ALREADY_EXISTS"
            )
        
        # Registra o parser
        self._parsers[parser_name] = parser
        
        # Reordena por prioridade
        self._rebuild_priority_list()
        
        # Habilita por padrão se o parser está habilitado
        if parser.is_enabled:
            self._enabled_parsers.add(parser_name)
        
        # Registra aliases
        if aliases:
            for alias in aliases:
                if alias in self._parser_aliases:
                    logger.warning(f"Alias '{alias}' already exists, overwriting")
                self._parser_aliases[alias] = parser_name
        
        # logger.info(f"Registered parser: {parser_name} (priority: {parser.priority})")
    
    def unregister(self, parser_name: str) -> bool:
        """Remove um parser do registry
        
        Args:
            parser_name: Nome do parser a ser removido
            
        Returns:
            bool: True se o parser foi removido com sucesso
        """
        if parser_name not in self._parsers:
            return False
        
        parser = self._parsers[parser_name]
        
        # Remove da lista ordenada
        self._parsers_by_priority.remove(parser)
        
        # Remove dos habilitados
        self._enabled_parsers.discard(parser_name)
        
        # Remove aliases
        aliases_to_remove = [
            alias for alias, name in self._parser_aliases.items()
            if name == parser_name
        ]
        for alias in aliases_to_remove:
            del self._parser_aliases[alias]
        
        # Remove o parser
        del self._parsers[parser_name]
        
        # Limpa cache relacionado
        self._clear_parser_cache(parser_name)
        
        # logger.info(f"Unregistered parser: {parser_name}")
        return True
    
    def get(self, parser_name: str) -> Optional[Parser]:
        """Obtém um parser pelo nome ou alias
        
        Args:
            parser_name: Nome ou alias do parser
            
        Returns:
            Parser: Instância do parser ou None se não encontrado
        """
        # Tenta pelo nome direto
        if parser_name in self._parsers:
            return self._parsers[parser_name]
        
        # Tenta pelos aliases
        if parser_name in self._parser_aliases:
            real_name = self._parser_aliases[parser_name]
            return self._parsers.get(real_name)
        
        return None
    
    def get_enabled(self, parser_name: str) -> Optional[Parser]:
        """Obtém um parser apenas se ele estiver habilitado
        
        Args:
            parser_name: Nome ou alias do parser
            
        Returns:
            Parser: Instância do parser se habilitado, None caso contrário
        """
        parser = self.get(parser_name)
        if parser and parser.is_enabled and parser.name in self._enabled_parsers:
            return parser
        return None
    
    def list_all(self) -> List[Parser]:
        """Lista todos os parsers registrados ordenados por prioridade"""
        return self._parsers_by_priority.copy()
    
    def list_enabled(self) -> List[Parser]:
        """Lista apenas os parsers habilitados ordenados por prioridade"""
        return [
            parser for parser in self._parsers_by_priority
            if parser.is_enabled and parser.name in self._enabled_parsers
        ]
    
    def enable_parser(self, parser_name: str) -> bool:
        """Habilita um parser
        
        Args:
            parser_name: Nome do parser
            
        Returns:
            bool: True se foi habilitado com sucesso
        """
        parser = self.get(parser_name)
        if not parser:
            return False
        
        parser.enable()
        self._enabled_parsers.add(parser.name)  # Usa o nome real, não o alias
        logger.info(f"Enabled parser: {parser.name}")
        return True
    
    def disable_parser(self, parser_name: str) -> bool:
        """Desabilita um parser
        
        Args:
            parser_name: Nome do parser
            
        Returns:
            bool: True se foi desabilitado com sucesso
        """
        parser = self.get(parser_name)
        if not parser:
            return False
        
        parser.disable()
        self._enabled_parsers.discard(parser.name)  # Usa o nome real, não o alias
        logger.info(f"Disabled parser: {parser.name}")
        return True
    
    async def parse(self, input_text: str, working_directory: Optional[str] = None) -> ParseResult:
        """Parseia entrada usando o melhor parser disponível
        
        Args:
            input_text: Entrada do usuário
            working_directory: Diretório de trabalho para parsers que precisam
            
        Returns:
            ParseResult: Resultado do parsing
        """
        if not input_text or not input_text.strip():
            return ParseResult(
                status=ParseStatus.FAILED,
                error_message="Empty input provided"
            )
        
        # Verifica cache se habilitado
        if self._cache_enabled:
            input_hash = hash(input_text)
            if input_hash in self._parse_cache:
                cached_parser, cached_result = self._parse_cache[input_hash]
                logger.debug(f"Cache hit for input (parser: {cached_parser})")
                return cached_result
        
        # Tenta parsers em ordem de prioridade
        enabled_parsers = self.list_enabled()
        if not enabled_parsers:
            return ParseResult(
                status=ParseStatus.FAILED,
                error_message="No enabled parsers available"
            )
        
        parse_results = []
        
        for parser in enabled_parsers:
            try:
                if parser.can_parse(input_text):
                    # Passa working_directory se o parser suporta
                    if hasattr(parser, 'parse_async') and 'working_directory' in parser.parse_async.__code__.co_varnames:
                        result = await parser.parse_async(input_text, working_directory=working_directory)
                    else:
                        result = await parser.parse_async(input_text)
                    
                    # Se obteve sucesso, cacheia e retorna
                    if result.is_success:
                        if self._cache_enabled:
                            self._parse_cache[hash(input_text)] = (parser.name, result)
                        return result
                    
                    # Se é parcial, mantém para consideração
                    if result.is_partial:
                        parse_results.append((parser, result))
                
            except Exception as e:
                logger.warning(f"Parser {parser.name} failed: {e}")
                parse_results.append((parser, ParseResult(
                    status=ParseStatus.FAILED,
                    error_message=f"Parser {parser.name} error: {str(e)}"
                )))
        
        # Se não houve sucesso completo, retorna o melhor resultado parcial
        if parse_results:
            # Ordena por confiança (maior primeiro)
            parse_results.sort(key=lambda x: x[1].confidence, reverse=True)
            best_parser, best_result = parse_results[0]
            
            if self._cache_enabled and best_result.is_partial:
                self._parse_cache[hash(input_text)] = (best_parser.name, best_result)
            
            return best_result
        
        # Nenhum parser conseguiu processar
        return ParseResult(
            status=ParseStatus.FAILED,
            error_message="No parser could handle the input"
        )
    
    async def find_suitable_parsers(self, input_text: str) -> List[Tuple[Parser, float]]:
        """Encontra parsers adequados com suas respectivas confianças
        
        Args:
            input_text: Entrada do usuário
            
        Returns:
            List[Tuple[Parser, float]]: Lista de (parser, confiança) ordenada por confiança
        """
        suitable_parsers = []
        
        for parser in self.list_enabled():
            try:
                confidence = parser.get_confidence(input_text)
                if confidence > 0.0:
                    suitable_parsers.append((parser, confidence))
            except Exception as e:
                logger.warning(f"Error getting confidence from parser {parser.name}: {e}")
        
        # Ordena por confiança (maior primeiro)
        suitable_parsers.sort(key=lambda x: x[1], reverse=True)
        return suitable_parsers
    
    async def get_suggestions(self, partial_input: str) -> Dict[str, List[str]]:
        """Obtém sugestões de autocompletar de todos os parsers
        
        Args:
            partial_input: Entrada parcial do usuário
            
        Returns:
            Dict[str, List[str]]: Sugestões por parser
        """
        all_suggestions = {}
        
        for parser in self.list_enabled():
            try:
                suggestions = await parser.get_suggestions(partial_input)
                if suggestions:
                    all_suggestions[parser.name] = suggestions
            except Exception as e:
                logger.debug(f"Parser {parser.name} failed to provide suggestions: {e}")
        
        return all_suggestions
    
    def auto_discover(self, package_names: Optional[List[str]] = None) -> int:
        """Descobre automaticamente parsers em pacotes
        
        Args:
            package_names: Lista de pacotes para descobrir (opcional)
            
        Returns:
            int: Número de parsers descobertos
        """
        if not self._auto_discovery_enabled:
            return 0
        
        if package_names is None:
            package_names = [
                'deile.parsers.file_parser',
                'deile.parsers.command_parser', 
                'deile.parsers.diff_parser',
                'deile.parsers.intelligent_file_parser'
            ]
        
        discovered_count = 0
        
        for package_name in package_names:
            try:
                discovered_count += self._discover_in_package(package_name)
            except Exception as e:
                logger.warning(f"Failed to discover parsers in {package_name}: {e}")
        
        return discovered_count
    
    def _discover_in_package(self, package_name: str) -> int:
        """Descobre parsers em um pacote específico"""
        try:
            module = importlib.import_module(package_name)
        except ImportError:
            logger.debug(f"Package {package_name} not found for auto-discovery")
            return 0
        
        discovered_count = 0
        
        # Procura por classes que herdam de Parser
        for name in dir(module):
            obj = getattr(module, name)
            if (
                inspect.isclass(obj) and
                issubclass(obj, Parser) and
                obj != Parser and
                not inspect.isabstract(obj)
            ):
                try:
                    # Instancia e registra o parser
                    parser_instance = obj()
                    if parser_instance.name not in self._parsers:
                        self.register(parser_instance)
                        discovered_count += 1
                except Exception as e:
                    logger.warning(f"Failed to register discovered parser {name}: {e}")
        
        return discovered_count
    
    def _rebuild_priority_list(self) -> None:
        """Reconstrói a lista ordenada por prioridade"""
        self._parsers_by_priority = sorted(
            self._parsers.values(),
            key=lambda p: p.priority,
            reverse=True
        )
    
    def _clear_parser_cache(self, parser_name: Optional[str] = None) -> None:
        """Limpa cache de parsing"""
        if parser_name:
            # Remove apenas entradas do parser específico
            keys_to_remove = [
                key for key, (cached_parser, _) in self._parse_cache.items()
                if cached_parser == parser_name
            ]
            for key in keys_to_remove:
                del self._parse_cache[key]
        else:
            # Limpa todo o cache
            self._parse_cache.clear()
    
    def get_stats(self) -> Dict[str, Any]:
        """Retorna estatísticas do registry"""
        total_parsers = len(self._parsers)
        enabled_parsers = len(self._enabled_parsers)
        
        priority_stats = {}
        for parser in self._parsers.values():
            priority = parser.priority
            if priority not in priority_stats:
                priority_stats[priority] = 0
            priority_stats[priority] += 1
        
        return {
            "total_parsers": total_parsers,
            "enabled_parsers": enabled_parsers,
            "disabled_parsers": total_parsers - enabled_parsers,
            "total_aliases": len(self._parser_aliases),
            "cache_enabled": self._cache_enabled,
            "cache_size": len(self._parse_cache),
            "priority_breakdown": priority_stats,
            "auto_discovery_enabled": self._auto_discovery_enabled
        }
    
    def enable_cache(self) -> None:
        """Habilita cache de parsing"""
        self._cache_enabled = True
    
    def disable_cache(self) -> None:
        """Desabilita e limpa cache de parsing"""
        self._cache_enabled = False
        self._parse_cache.clear()
    
    def clear_cache(self) -> None:
        """Limpa cache de parsing"""
        self._parse_cache.clear()
    
    def clear(self) -> None:
        """Limpa todos os parsers registrados"""
        self._parsers.clear()
        self._parsers_by_priority.clear()
        self._enabled_parsers.clear()
        self._parser_aliases.clear()
        self._parse_cache.clear()
        logger.info("Cleared all parsers from registry")
    
    def disable_auto_discovery(self) -> None:
        """Desabilita descoberta automática"""
        self._auto_discovery_enabled = False
    
    def enable_auto_discovery(self) -> None:
        """Habilita descoberta automática"""
        self._auto_discovery_enabled = True
    
    def __len__(self) -> int:
        return len(self._parsers)
    
    def __contains__(self, parser_name: str) -> bool:
        return parser_name in self._parsers or parser_name in self._parser_aliases
    
    def __iter__(self):
        return iter(self._parsers_by_priority)


# Singleton instance
_parser_registry: Optional[ParserRegistry] = None


def get_parser_registry() -> ParserRegistry:
    """Retorna a instância singleton do ParserRegistry"""
    global _parser_registry
    if _parser_registry is None:
        _parser_registry = ParserRegistry()
    return _parser_registry


def register_parser(parser: Parser, aliases: Optional[List[str]] = None) -> None:
    """Função helper para registrar um parser"""
    registry = get_parser_registry()
    registry.register(parser, aliases)