"""Loader dinâmico para personas com suporte a diferentes tipos"""

import logging
import importlib
import inspect
from pathlib import Path
from typing import Dict, Any, Type, Optional
import yaml

from .base import BasePersona, PersonaConfig

logger = logging.getLogger(__name__)


class PersonaLoader:
    """Carregador dinâmico de personas

    Suporta:
    - Personas configuradas via YAML
    - Personas customizadas via código Python
    - Validação automática
    - Cache de classes carregadas
    """

    def __init__(self):
        self._persona_classes: Dict[str, Type[BasePersona]] = {}
        self._class_cache: Dict[str, Type[BasePersona]] = {}

    async def load_persona(self, config: PersonaConfig) -> BasePersona:
        """Carrega uma persona baseada na configuração

        Args:
            config: Configuração da persona

        Returns:
            BasePersona: Instância da persona carregada
        """
        try:
            # Tenta carregar classe customizada primeiro
            persona_class = await self._get_persona_class(config)

            # Cria instância da persona
            persona = persona_class(config)

            # Valida a configuração
            errors = await persona.validate_config()
            if errors:
                logger.warning(f"Persona '{config.name}' tem erros de validação: {errors}")

            logger.debug(f"Persona '{config.name}' carregada com classe {persona_class.__name__}")
            return persona

        except Exception as e:
            logger.error(f"Erro ao carregar persona '{config.name}': {e}")
            raise

    async def _get_persona_class(self, config: PersonaConfig) -> Type[BasePersona]:
        """Obtém a classe de persona apropriada

        Args:
            config: Configuração da persona

        Returns:
            Type[BasePersona]: Classe da persona
        """
        # Verifica se há classe customizada especificada
        if hasattr(config, 'custom_class') and config.custom_class:
            return await self._load_custom_class(config.custom_class)

        # Tenta carregar por convenção (persona_id -> classe)
        class_name = self._persona_id_to_class_name(config.persona_id)
        try:
            return await self._load_persona_class_by_name(class_name)
        except (ImportError, AttributeError):
            pass

        # Fallback para persona genérica
        return await self._get_generic_persona_class()

    def _persona_id_to_class_name(self, persona_id: str) -> str:
        """Converte persona_id para nome de classe

        Args:
            persona_id: ID da persona (ex: 'developer', 'architect')

        Returns:
            str: Nome da classe (ex: 'DeveloperPersona', 'ArchitectPersona')
        """
        # Converte snake_case para PascalCase + 'Persona'
        words = persona_id.replace('-', '_').split('_')
        class_name = ''.join(word.capitalize() for word in words) + 'Persona'
        return class_name

    async def _load_persona_class_by_name(self, class_name: str) -> Type[BasePersona]:
        """Carrega classe de persona por nome

        Args:
            class_name: Nome da classe a carregar

        Returns:
            Type[BasePersona]: Classe da persona
        """
        if class_name in self._class_cache:
            return self._class_cache[class_name]

        # Tenta importar de diferentes módulos
        possible_modules = [
            f'deile.personas.types.{class_name.lower()}',
            f'deile.personas.custom.{class_name.lower()}',
            f'deile.personas.{class_name.lower()}'
        ]

        for module_name in possible_modules:
            try:
                module = importlib.import_module(module_name)
                if hasattr(module, class_name):
                    persona_class = getattr(module, class_name)
                    if self._validate_persona_class(persona_class):
                        self._class_cache[class_name] = persona_class
                        return persona_class
            except ImportError:
                continue

        raise ImportError(f"Classe {class_name} não encontrada nos módulos disponíveis")

    async def _load_custom_class(self, class_path: str) -> Type[BasePersona]:
        """Carrega classe customizada especificada por caminho

        Args:
            class_path: Caminho para a classe (ex: 'my_module.MyPersonaClass')

        Returns:
            Type[BasePersona]: Classe da persona
        """
        if class_path in self._class_cache:
            return self._class_cache[class_path]

        try:
            module_path, class_name = class_path.rsplit('.', 1)
            module = importlib.import_module(module_path)
            persona_class = getattr(module, class_name)

            if not self._validate_persona_class(persona_class):
                raise TypeError(f"Classe {class_path} não é uma persona válida")

            self._class_cache[class_path] = persona_class
            return persona_class

        except (ImportError, AttributeError, ValueError) as e:
            raise ImportError(f"Erro ao carregar classe customizada {class_path}: {e}")

    async def _get_generic_persona_class(self) -> Type[BasePersona]:
        """Retorna classe de persona genérica como fallback"""
        if 'GenericPersona' in self._class_cache:
            return self._class_cache['GenericPersona']

        # Cria classe genérica dinamicamente
        class GenericPersona(BasePersona):
            """Persona genérica que implementa comportamento básico"""

            async def build_system_instruction(self, context: Dict[str, Any] = None) -> str:
                """Constrói system instruction básica"""
                base = self.config.system_instruction

                # Adiciona contexto se disponível
                if context:
                    context_info = []
                    if 'working_directory' in context:
                        context_info.append(f"Diretório de trabalho: {context['working_directory']}")
                    if 'session_info' in context:
                        context_info.append(f"Sessão: {context['session_info']}")

                    if context_info:
                        base += "\n\nContexto adicional:\n" + "\n".join(context_info)

                return base

            async def process_user_input(self, user_input: str, context: Dict[str, Any] = None) -> str:
                """Processa input do usuário com estilo básico"""
                # Para persona genérica, retorna input sem modificações
                # Personas específicas podem implementar processamento customizado
                return user_input

        self._class_cache['GenericPersona'] = GenericPersona
        return GenericPersona

    def _validate_persona_class(self, persona_class: Type) -> bool:
        """Valida se uma classe é uma persona válida

        Args:
            persona_class: Classe a validar

        Returns:
            bool: True se é uma persona válida
        """
        try:
            # Verifica se herda de BasePersona
            if not issubclass(persona_class, BasePersona):
                return False

            # Verifica se implementa métodos abstratos
            required_methods = ['build_system_instruction', 'process_user_input']
            for method in required_methods:
                if not hasattr(persona_class, method):
                    return False

                method_obj = getattr(persona_class, method)
                if not callable(method_obj):
                    return False

            return True

        except TypeError:
            # Não é uma classe válida
            return False

    def register_persona_class(self, persona_id: str, persona_class: Type[BasePersona]) -> None:
        """Registra uma classe de persona manualmente

        Args:
            persona_id: ID da persona
            persona_class: Classe da persona
        """
        if not self._validate_persona_class(persona_class):
            raise TypeError("Classe fornecida não é uma persona válida")

        self._persona_classes[persona_id] = persona_class
        logger.debug(f"Classe de persona registrada: {persona_id} -> {persona_class.__name__}")

    def list_available_classes(self) -> Dict[str, str]:
        """Lista todas as classes de persona disponíveis

        Returns:
            Dict[str, str]: Mapeamento persona_id -> nome da classe
        """
        return {
            persona_id: cls.__name__
            for persona_id, cls in self._persona_classes.items()
        }

    async def discover_persona_modules(self, search_paths: list = None) -> int:
        """Descobre automaticamente módulos de personas

        Args:
            search_paths: Caminhos para buscar (padrão: deile/personas/types/)

        Returns:
            int: Número de classes descobertas
        """
        if search_paths is None:
            search_paths = [Path("deile/personas/types")]

        discovered = 0

        for search_path in search_paths:
            if isinstance(search_path, str):
                search_path = Path(search_path)

            if not search_path.exists():
                continue

            # Busca por arquivos Python
            for py_file in search_path.glob("*.py"):
                if py_file.name.startswith("_"):
                    continue

                try:
                    # Converte caminho para nome de módulo
                    module_parts = list(py_file.with_suffix("").parts)
                    module_name = ".".join(module_parts)

                    module = importlib.import_module(module_name)

                    # Procura por classes de persona no módulo
                    for name, obj in inspect.getmembers(module, inspect.isclass):
                        if (name != 'BasePersona' and
                            issubclass(obj, BasePersona) and
                            not inspect.isabstract(obj)):

                            # Gera persona_id baseado no nome da classe
                            persona_id = self._class_name_to_persona_id(name)
                            self.register_persona_class(persona_id, obj)
                            discovered += 1

                except Exception as e:
                    logger.warning(f"Erro ao processar {py_file}: {e}")

        logger.info(f"Descobertas {discovered} classes de persona automaticamente")
        return discovered

    def _class_name_to_persona_id(self, class_name: str) -> str:
        """Converte nome de classe para persona_id

        Args:
            class_name: Nome da classe (ex: 'DeveloperPersona')

        Returns:
            str: persona_id (ex: 'developer')
        """
        # Remove sufixo 'Persona' se presente
        if class_name.endswith('Persona'):
            class_name = class_name[:-7]

        # Converte PascalCase para snake_case
        result = []
        for i, char in enumerate(class_name):
            if i > 0 and char.isupper():
                result.append('_')
            result.append(char.lower())

        return ''.join(result)