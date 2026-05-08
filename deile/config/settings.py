"""Sistema de configurações do DEILE.

Layered settings (issue #111):
  Project ``.deile/settings.json`` > User ``~/.deile/settings.json`` > defaults.

The legacy ``config/settings.json`` flow is still recognized as a one-shot
fallback (with a deprecation log). New writes go to the user's
``~/.deile/settings.json`` via :class:`SettingsManager`.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LogLevel(Enum):
    """Níveis de log disponíveis"""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Override mapping (issue #111)
# ---------------------------------------------------------------------------

def _to_log_level(value: Any) -> LogLevel:
    if isinstance(value, LogLevel):
        return value
    return LogLevel(str(value).upper())


def _to_optional_path(value: Any) -> Optional[Path]:
    if value in (None, ""):
        return None
    return Path(str(value)).expanduser()


def _mb_to_bytes(value: Any) -> int:
    return int(value) * 1024 * 1024


# Map of nested JSON paths in ``.deile/settings.json`` to ``Settings`` flat
# fields, with a converter for each. Anything not listed is ignored (with a
# debug log) — that's how unknown-but-future keys stay forward-compatible.
_OVERRIDE_HANDLERS: Dict[str, Tuple[str, Callable[[Any], Any]]] = {
    "logging.level": ("log_level", _to_log_level),
    "logging.to_file": ("log_to_file", bool),
    "logging.max_size_mb": ("log_file_max_size", _mb_to_bytes),
    "logging.backup_count": ("log_file_backup_count", int),
    "ui.streaming_enabled": ("streaming_enabled", bool),
    "ui.show_tool_details": ("show_tool_details", bool),
    "model.default_provider": ("default_model_provider", str),
    "model.max_context_tokens": ("max_context_tokens", int),
    "caching.enabled": ("enable_caching", bool),
    "caching.ttl_seconds": ("cache_ttl", int),
    "caching.parser_cache_enabled": ("parser_cache_enabled", bool),
    "caching.parser_cache_ttl": ("parser_cache_ttl", int),
    "concurrency.max_concurrent_requests": ("max_concurrent_requests", int),
    "concurrency.request_timeout": ("request_timeout", int),
    "concurrency.max_tool_execution_time": ("max_tool_execution_time", int),
    "file_safety.enabled": ("enable_file_safety_checks", bool),
    "file_safety.allowed_extensions": ("allowed_file_extensions", list),
    "file_safety.blocked_directories": ("blocked_directories", list),
    "file_safety.max_file_size_bytes": ("max_file_size_bytes", int),
    "file_safety.allow_all_types": ("allow_all_file_types", bool),
    "file_safety.encoding_detection": ("file_encoding_detection", bool),
    "deile_md.enabled": ("deile_md_enabled", bool),
    "deile_md.user_path": ("deile_md_user_path", _to_optional_path),
    "deile_md.cwd_filename": ("deile_md_cwd_filename", str),
    "deile_md.max_bytes": ("deile_md_max_bytes", int),
    "environment": ("environment", str),
    "debug": ("debug", bool),
}


def _resolve_dotted(data: dict, key_path: str) -> Tuple[bool, Any]:
    """Walk *data* by dotted *key_path*. Returns (found, value)."""
    node: Any = data
    for part in key_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


@dataclass
class Settings:
    """Configurações globais do DEILE"""
    
    # Configurações básicas
    app_name: str = "DEILE"
    version: str = "5.1.0"
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

    # Streaming UI
    streaming_enabled: bool = True
    show_tool_details: bool = False  # When True, show full tool payload after the turn
    
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

    # DEILE.md hierarchical loader (Issue #62 / Feature #64)
    deile_md_enabled: bool = True
    deile_md_user_path: Optional[Path] = None  # default: ~/.deile/DEILE.md
    deile_md_cwd_filename: str = "DEILE.md"
    deile_md_max_bytes: int = 64 * 1024  # cap per-layer to avoid bloat
    
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
    
    def _load_api_keys_from_env(self) -> Dict[str, str]:
        """Carrega API keys das variáveis de ambiente"""
        api_keys = {}
        
        # Lista de API keys conhecidas
        known_keys = [
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AZURE_API_KEY",
            "DEEPSEEK_API_KEY",
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
        """DEPRECATED (issue #111): write to ``~/.deile/settings.json`` instead.

        Kept for backward compat with callers that pass an explicit
        *file_path*. When called without arguments, writes to the legacy
        ``config/settings.json`` path with a deprecation log.
        """
        if file_path is None:
            file_path = self.config_directory / "settings.json"
            logger.warning(
                "Settings.save_to_file() with no path is deprecated; "
                "writes to legacy %s. Migrate to ~/.deile/settings.json.",
                file_path,
            )

        try:
            config_dict = self.to_dict()

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(config_dict, f, indent=2, default=str)

            logger.info("Settings saved to %s", file_path)
            return True

        except OSError as e:
            logger.error("Failed to save settings to %s: %s", file_path, e)
            return False

    @classmethod
    def load_from_file(cls, file_path: Path) -> 'Settings':
        """DEPRECATED (issue #111): read via SettingsManager + apply_overrides.

        Kept so existing callers passing an explicit path don't break;
        ``get_settings()`` no longer routes through this method.
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                config_dict = json.load(f)

            if "api_keys" in config_dict:
                logger.warning("API keys found in config file. Ignoring them for security.")
                del config_dict["api_keys"]

            for key in ("working_directory", "config_directory", "logs_directory", "cache_directory"):
                if key in config_dict:
                    config_dict[key] = Path(config_dict[key])

            if "log_level" in config_dict:
                config_dict["log_level"] = LogLevel(config_dict["log_level"])

            settings = cls(**config_dict)
            logger.info("Settings loaded from %s", file_path)
            return settings

        except (OSError, ValueError, TypeError) as e:
            logger.warning("Failed to load settings from %s: %s", file_path, e)
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
                logger.warning("Unknown setting: %s", key)

    def apply_overrides(self, data: dict) -> None:
        """Apply nested ``.deile/settings.json`` overrides to flat fields.

        Walks ``_OVERRIDE_HANDLERS`` and pulls each known key out of *data*.
        Unknown keys are ignored (logged at debug). Convertion errors fall
        back to leaving the existing default in place plus a warning log.
        """
        if not isinstance(data, dict) or not data:
            return
        for key_path, (field_name, converter) in _OVERRIDE_HANDLERS.items():
            found, raw = _resolve_dotted(data, key_path)
            if not found:
                continue
            try:
                value = converter(raw)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "settings: cannot apply %s=%r (%s); keeping default",
                    key_path, raw, exc,
                )
                continue
            setattr(self, field_name, value)
    
    def get_api_key(self, provider: str) -> Optional[str]:
        """Obtém API key para um provedor específico"""
        key_mapping = {
            "gemini": "GOOGLE_API_KEY",
            "google": "GOOGLE_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gpt": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "azure": "AZURE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
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


def _load_layered_settings() -> Settings:
    """Build a fresh ``Settings`` from defaults + ``.deile/settings.json`` layers.

    Order:
      1. Defaults from the dataclass.
      2. Apply ``~/.deile/settings.json`` (user) overrides.
      3. Apply ``<cwd>/.deile/settings.json`` (project) overrides on top.

    If neither layer exists but the legacy ``config/settings.json`` is
    present, fall back to it once with a deprecation warning. This
    preserves behavior for setups that haven't migrated yet.
    """
    settings = Settings()

    try:
        from ..commands.settings_manager import SettingsManager
    except ImportError as exc:  # pragma: no cover — circular import guard
        logger.warning("settings: SettingsManager unavailable (%s); using defaults", exc)
        return settings

    manager = SettingsManager()
    user_layer = manager.get_layer(SettingsManager.GLOBAL)
    project_layer = manager.get_layer(SettingsManager.PROJECT)

    if not user_layer and not project_layer:
        legacy_path = Path.cwd() / "config" / "settings.json"
        if legacy_path.exists():
            logger.warning(
                "settings: loading legacy %s; migrate to ~/.deile/settings.json (issue #111)",
                legacy_path,
            )
            return Settings.load_from_file(legacy_path)
        return settings

    if user_layer:
        settings.apply_overrides(user_layer)
    if project_layer:
        settings.apply_overrides(project_layer)
    return settings


def get_settings() -> Settings:
    """Retorna instância singleton das configurações.

    Reads ``.deile/settings.json`` (project > user > defaults). The legacy
    ``config/settings.json`` is honored as a one-shot fallback when neither
    new-layer file exists.
    """
    global _settings

    if _settings is None:
        _settings = _load_layered_settings()

    return _settings


def update_settings(**kwargs) -> None:
    """Atualiza configurações globais"""
    settings = get_settings()
    settings.update(**kwargs)


def reset_settings() -> None:
    """Reseta configurações para padrão"""
    global _settings
    _settings = None