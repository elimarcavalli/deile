"""Sistema de configurações do DEILE"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from pathlib import Path
import os
import json
import logging
from enum import Enum


logger = logging.getLogger(__name__)


class LogLevel(Enum):
    """Níveis de log disponíveis"""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@dataclass
class Settings:
    """Configurações globais do DEILE"""
    
    # Configurações básicas
    app_name: str = "DEILE"
    version: str = "5.0.0"
    debug: bool = False
    
    # Configurações de diretórios
    working_directory: Path = field(default_factory=Path.cwd)
    config_directory: Path = field(default_factory=lambda: Path.cwd() / "config")
    logs_directory: Path = field(default_factory=lambda: Path.cwd() / "logs")
    cache_directory: Path = field(default_factory=lambda: Path.cwd() / "cache")
    
    # Configurações de logging
    log_level: LogLevel = LogLevel.DEBUG
    log_to_file: bool = True
    log_file_max_size: int = 10 * 1024 * 1024  # 10MB
    log_file_backup_count: int = 5
    
    # Configurações de modelo (DELEGADAS PARA ConfigManager)
    default_model_provider: str = "gemini"  # Apenas para fallback se ConfigManager falhar
    default_model_name: str = "gemini-1.5-pro-latest"
    max_context_tokens: int = 8000
    
    # Configurações de tools
    auto_discover_tools: bool = True
    enabled_tool_categories: List[str] = field(default_factory=lambda: ["file", "execution", "search"])
    max_tool_execution_time: int = 30  # segundos
    
    # Configurações de parsers
    auto_discover_parsers: bool = True
    parser_cache_enabled: bool = True
    parser_cache_ttl: int = 300  # 5 minutos
    
    # Configurações de contexto (DELEGADAS PARA ConfigManager)
    # Mantidas aqui apenas como fallback para infraestrutura básica
    context_optimization_enabled: bool = True
    rag_enabled: bool = False
    semantic_search_enabled: bool = False
    embedding_model: str = "text-embedding-ada-002"
    
    # Configurações de performance
    max_concurrent_requests: int = 10
    request_timeout: int = 120
    enable_caching: bool = True
    cache_ttl: int = 3600  # 1 hora
    
    # Configurações de segurança
    enable_file_safety_checks: bool = True
    allowed_file_extensions: List[str] = field(default_factory=lambda: [
        ".py", ".js", ".ts", ".html", ".css", ".md", ".txt", ".json", ".yaml", ".yml"
    ])
    blocked_directories: List[str] = field(default_factory=lambda: [
        ".git", "__pycache__", "node_modules", ".env"
    ])

    # Configurações de leitura de arquivos
    max_file_size_bytes: int = 1024 * 1024  # 1MB padrão
    allow_all_file_types: bool = True  # Permite qualquer tipo de arquivo
    file_encoding_detection: bool = True  # Detecta encoding automaticamente
    
    # Configurações específicas do ambiente
    environment: str = "development"  # development, staging, production
    api_keys: Dict[str, str] = field(default_factory=dict)
    
    def __post_init__(self):
        """Inicialização pós-criação"""
        # Converte strings para Path objects
        if isinstance(self.working_directory, str):
            self.working_directory = Path(self.working_directory)
        if isinstance(self.config_directory, str):
            self.config_directory = Path(self.config_directory)
        if isinstance(self.logs_directory, str):
            self.logs_directory = Path(self.logs_directory)
        if isinstance(self.cache_directory, str):
            self.cache_directory = Path(self.cache_directory)
        
        # Carrega API keys do ambiente se não fornecidas
        if not self.api_keys:
            self.api_keys = self._load_api_keys_from_env()
        
        # Cria diretórios se não existem
        self._create_directories()
    
    def _load_api_keys_from_env(self) -> Dict[str, str]:
        """Carrega API keys das variáveis de ambiente"""
        api_keys = {}
        
        # Lista de API keys conhecidas
        known_keys = [
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY", 
            "ANTHROPIC_API_KEY",
            "AZURE_API_KEY"
        ]
        
        for key in known_keys:
            value = os.getenv(key)
            if value:
                api_keys[key] = value
        
        return api_keys
    
    def _create_directories(self) -> None:
        """Cria diretórios necessários"""
        directories = [
            self.config_directory,
            self.logs_directory,
            self.cache_directory
        ]
        
        for directory in directories:
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.warning(f"Could not create directory {directory}: {e}")
    
    def save_to_file(self, file_path: Optional[Path] = None) -> bool:
        """Salva configurações em arquivo JSON"""
        if file_path is None:
            file_path = self.config_directory / "settings.json"
        
        try:
            # Converte para dicionário serializável
            config_dict = self.to_dict()
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(config_dict, f, indent=2, default=str)
            
            logger.info(f"Settings saved to {file_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save settings to {file_path}: {e}")
            return False
    
    @classmethod
    def load_from_file(cls, file_path: Path) -> 'Settings':
        """Carrega configurações de arquivo JSON"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                config_dict = json.load(f)

            if 'api_keys' in config_dict:
                logger.warning("API keys found in config file. Ignoring them for security.")
                del config_dict['api_keys']

            # Converte paths de volta para Path objects
            for key in ['working_directory', 'config_directory', 'logs_directory', 'cache_directory']:
                if key in config_dict:
                    config_dict[key] = Path(config_dict[key])

            # Converte log_level de volta para enum
            if 'log_level' in config_dict:
                config_dict['log_level'] = LogLevel(config_dict['log_level'])

            settings = cls(**config_dict)
            logger.info(f"Settings loaded from {file_path}")
            return settings
            
        except Exception as e:
            logger.warning(f"Failed to load settings from {file_path}: {e}")
            logger.info("Using default settings")
            return cls()
    
    def to_dict(self, exclude_api_keys: bool = True) -> Dict[str, Any]:
        """Converte configurações para dicionário

        Args:
            exclude_api_keys: Se True, exclui API keys do dicionário (padrão para salvar em arquivo)
        """
        result = {}

        for key, value in self.__dict__.items():
            # SEGURANÇA: Nunca salva API keys em arquivo por padrão
            if exclude_api_keys and key == 'api_keys':
                continue

            if isinstance(value, Path):
                result[key] = str(value)
            elif isinstance(value, LogLevel):
                result[key] = value.value
            else:
                result[key] = value

        return result
    
    def update(self, **kwargs) -> None:
        """Atualiza configurações"""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.warning(f"Unknown setting: {key}")
    
    def get_api_key(self, provider: str) -> Optional[str]:
        """Obtém API key para um provedor específico"""
        key_mapping = {
            "gemini": "GOOGLE_API_KEY",
            "google": "GOOGLE_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gpt": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "azure": "AZURE_API_KEY"
        }
        
        key_name = key_mapping.get(provider.lower())
        if key_name:
            return self.api_keys.get(key_name)
        
        return None
    
    def is_development(self) -> bool:
        """Verifica se está em ambiente de desenvolvimento"""
        return self.environment == "development"
    
    def is_production(self) -> bool:
        """Verifica se está em ambiente de produção"""
        return self.environment == "production"
    
    def get_config_manager(self):
        """Obtém ConfigManager como fonte da verdade para configs de modelo/agent"""
        try:
            from .manager import get_config_manager
            return get_config_manager()
        except ImportError:
            logger.warning("ConfigManager not available, using fallback settings")
            return None
    
    def get_model_config(self) -> Dict[str, Any]:
        """Obtém configurações de modelo via ConfigManager"""
        config_manager = self.get_config_manager()
        if config_manager:
            config = config_manager.get_config()
            return {
                "model_name": config.gemini.model_name,
                "temperature": config.gemini.generation_config.get("temperature", 0.1),
                "max_context_tokens": config.agent.max_context_tokens,
                "generation_config": config.gemini.generation_config,
                "tool_config": config.gemini.tool_config,
                "safety_settings": config.gemini.safety_settings
            }
        return {
            "model_name": "gemini-1.5-pro-latest",
            "temperature": 0.1,
            "max_context_tokens": 8000
        }
    
    def validate(self) -> List[str]:
        """Valida configurações e retorna lista de problemas"""
        issues = []
        
        # Verifica API keys essenciais
        if not self.get_api_key(self.default_model_provider):
            issues.append(f"Missing API key for default provider: {self.default_model_provider}")
        
        # Verifica diretórios
        if not self.working_directory.exists():
            issues.append(f"Working directory does not exist: {self.working_directory}")
        
        # Valida através do ConfigManager se disponível
        config_manager = self.get_config_manager()
        if config_manager:
            try:
                config = config_manager.get_config()
                config_issues = config.validate()
                issues.extend(config_issues)
            except Exception as e:
                issues.append(f"ConfigManager validation failed: {e}")
        
        if self.max_concurrent_requests <= 0:
            issues.append("max_concurrent_requests must be positive")
        
        return issues
    
    def __str__(self) -> str:
        return f"Settings(env={self.environment}, provider={self.default_model_provider})"
    
    def __repr__(self) -> str:
        return f"<Settings: {self.app_name} v{self.version}>"


# Singleton instance
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Retorna instância singleton das configurações"""
    global _settings
    
    if _settings is None:
        # Tenta carregar de arquivo
        config_file = Path.cwd() / "config" / "settings.json"
        if config_file.exists():
            _settings = Settings.load_from_file(config_file)
        else:
            _settings = Settings()
            # Salva configurações padrão
            _settings.save_to_file()
    
    return _settings


def update_settings(**kwargs) -> None:
    """Atualiza configurações globais"""
    settings = get_settings()
    settings.update(**kwargs)


def reset_settings() -> None:
    """Reseta configurações para padrão"""
    global _settings
    _settings = None