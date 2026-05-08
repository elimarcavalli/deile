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
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEILE_DEPRECATED_ENV_VARS: Dict[str, str] = {
    "DEILE_PREFERRED_MODEL": "model.preferred",
    "DEILE_VISION_MODEL": "model.vision_model",
    "DEILE_BOT_APPROVAL_AUTO": "approval.auto",
    "DEILE_LOOP_GUARD_DISABLE": "loop_guard.disabled",
    "DEILE_LOOP_GUARD_MAX_CALLS": "loop_guard.max_calls",
    "DEILE_LOOP_GUARD_REPEAT_THRESHOLD": "loop_guard.repeat_threshold",
    "DEILE_LOOP_GUARD_WINDOW_SIZE": "loop_guard.window_size",
    "DEILE_LOOP_GUARD_WINDOW_THRESHOLD": "loop_guard.window_threshold",
    "DEILE_LOOP_GUARD_NO_PROGRESS": "loop_guard.no_progress",
    "DEILE_PIPELINE_BASE_PATH": "pipeline.base_path",
    "DEILE_PIPELINE_REPO": "pipeline.repo",
    "DEILE_PIPELINE_NOTIFY_USER_ID": "pipeline.notify_user_id",
    "DEILE_PIPELINE_POLL_INTERVAL": "pipeline.poll_interval",
    "DEILE_PIPELINE_CLAUDE_TIMEOUT": "pipeline.claude_timeout",
    "DEILE_PIPELINE_AUTOSTART": "pipeline.autostart",
    "DEILE_CRON_DB_PATH": "cron.db_path",
    "DEILE_CRON_POLL_INTERVAL": "cron.poll_interval",
    "DEILE_DEBUG": "debug.enabled",
}


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


def _to_bool(value: Any) -> bool:
    """Strict bool coercion — rejects ambiguous string literals."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.lower().strip()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"Cannot coerce {value!r} to bool")
    raise TypeError(f"Expected bool, got {type(value).__name__}")


def _to_str_list(value: Any) -> list:
    """Strict list coercion — rejects non-list values."""
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TypeError(f"Expected list, got {type(value).__name__}")


def _to_optional_path(value: Any) -> Optional[Path]:
    """Convert value to Path, hardened against null bytes and oversized values."""
    if value is None or (isinstance(value, str) and not value):
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"path setting must be string, got {type(value).__name__}")
    s = str(value)
    if "\x00" in s:
        raise ValueError("path setting contains null byte")
    if len(s) > 4096:
        raise ValueError(f"path setting exceeds 4096 chars (got {len(s)})")
    return Path(s).expanduser()


def _mb_to_bytes(value: Any) -> int:
    return int(value) * 1024 * 1024


# Map of nested JSON paths in ``.deile/settings.json`` to ``Settings`` flat
# fields, with a converter for each. Unknown keys are silently ignored —
# that's how future-compatible forward-compat works.
_OVERRIDE_HANDLERS: Dict[str, Tuple[str, Callable[[Any], Any]]] = {
    "logging.level": ("log_level", _to_log_level),
    "logging.to_file": ("log_to_file", _to_bool),
    "logging.max_size_mb": ("log_file_max_size", _mb_to_bytes),
    "logging.backup_count": ("log_file_backup_count", int),
    "ui.streaming_enabled": ("streaming_enabled", _to_bool),
    "ui.show_tool_details": ("show_tool_details", _to_bool),
    "model.default_provider": ("default_model_provider", str),
    "model.max_context_tokens": ("max_context_tokens", int),
    "caching.enabled": ("enable_caching", _to_bool),
    "caching.ttl_seconds": ("cache_ttl", int),
    "caching.parser_cache_enabled": ("parser_cache_enabled", _to_bool),
    "caching.parser_cache_ttl": ("parser_cache_ttl", int),
    "concurrency.max_concurrent_requests": ("max_concurrent_requests", int),
    "concurrency.request_timeout": ("request_timeout", int),
    "concurrency.max_tool_execution_time": ("max_tool_execution_time", int),
    "file_safety.enabled": ("enable_file_safety_checks", _to_bool),
    "file_safety.allowed_extensions": ("allowed_file_extensions", _to_str_list),
    "file_safety.blocked_directories": ("blocked_directories", _to_str_list),
    "file_safety.max_file_size_bytes": ("max_file_size_bytes", int),
    "file_safety.allow_all_types": ("allow_all_file_types", _to_bool),
    "file_safety.encoding_detection": ("file_encoding_detection", _to_bool),
    "deile_md.enabled": ("deile_md_enabled", _to_bool),
    "deile_md.user_path": ("deile_md_user_path", _to_optional_path),
    "deile_md.cwd_filename": ("deile_md_cwd_filename", str),
    "deile_md.max_bytes": ("deile_md_max_bytes", int),
    "environment": ("environment", str),
    "debug": ("debug", _to_bool),
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

    # Básicas
    app_name: str = "DEILE"
    version: str = "5.1.0"
    debug: bool = False

    # Diretórios (cwd-bound, não configuráveis via JSON)
    working_directory: Path = field(default_factory=Path.cwd)
    config_directory: Path = field(default_factory=lambda: Path.cwd() / "config")
    logs_directory: Path = field(default_factory=lambda: Path.cwd() / "logs")
    cache_directory: Path = field(default_factory=lambda: Path.cwd() / "cache")

    # Logging
    log_level: LogLevel = LogLevel.DEBUG
    log_to_file: bool = True
    log_file_max_size: int = 10 * 1024 * 1024
    log_file_backup_count: int = 5

    # Modelo (DELEGADO ao ConfigManager; mantido como fallback)
    default_model_provider: str = "gemini"
    default_model_name: str = "gemini-1.5-pro-latest"
    max_context_tokens: int = 8000
    preferred_model: Optional[str] = None
    vision_model: str = "gemini-2.5-flash-lite"

    # Tools
    auto_discover_tools: bool = True
    enabled_tool_categories: List[str] = field(
        default_factory=lambda: ["file", "execution", "search"]
    )
    max_tool_execution_time: int = 30

    # Parsers
    auto_discover_parsers: bool = True
    parser_cache_enabled: bool = True
    parser_cache_ttl: int = 300

    # Contexto (DELEGADO ao ConfigManager)
    context_optimization_enabled: bool = True
    rag_enabled: bool = False
    semantic_search_enabled: bool = False

    # Performance
    max_concurrent_requests: int = 10
    request_timeout: int = 120
    enable_caching: bool = True
    cache_ttl: int = 3600

    # Streaming UI
    streaming_enabled: bool = True
    show_tool_details: bool = False

    # Segurança de arquivos
    enable_file_safety_checks: bool = True
    allowed_file_extensions: List[str] = field(
        default_factory=lambda: [
            ".py",
            ".js",
            ".ts",
            ".html",
            ".css",
            ".md",
            ".txt",
            ".json",
            ".yaml",
            ".yml",
        ]
    )
    blocked_directories: List[str] = field(
        default_factory=lambda: [".git", "__pycache__", "node_modules", ".env"]
    )
    max_file_size_bytes: int = 1024 * 1024
    allow_all_file_types: bool = True
    file_encoding_detection: bool = True

    # Ambiente
    environment: str = "development"
    api_keys: Dict[str, str] = field(default_factory=dict)

    # DEILE.md hierarchical loader
    deile_md_enabled: bool = True
    deile_md_user_path: Optional[Path] = None
    deile_md_cwd_filename: str = "DEILE.md"
    deile_md_max_bytes: int = 64 * 1024

    # Loop guard
    loop_guard_disabled: bool = False
    loop_guard_max_calls: int = 50
    loop_guard_repeat_threshold: int = 3
    loop_guard_window_size: int = 5
    loop_guard_window_threshold: int = 3
    loop_guard_no_progress: int = 6

    # Aprovação de bots
    bot_approval_auto: bool = False

    # Debug
    debug_enabled: bool = False

    # Pipeline
    pipeline_base_path: Optional[Path] = None
    pipeline_repo: str = "elimarcavalli/deile"
    pipeline_notify_user_id: Optional[str] = None
    pipeline_poll_interval: int = 60
    pipeline_claude_timeout: int = 1800
    pipeline_autostart: bool = False  # gap #3: set True via DEILE_PIPELINE_AUTOSTART

    # Cron
    cron_db_path: Optional[Path] = None
    cron_poll_interval: int = 30

    # Perfil e skills (lidos via SettingsManager, mantidos aqui para conveniência)
    profile_name: str = "autonomous_agent"
    skills_paths: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for attr in ("working_directory", "config_directory", "logs_directory", "cache_directory"):
            v = getattr(self, attr)
            if isinstance(v, str):
                setattr(self, attr, Path(v))
        for attr in ("pipeline_base_path", "cron_db_path", "deile_md_user_path"):
            v = getattr(self, attr)
            if isinstance(v, str):
                setattr(self, attr, Path(v))

        if not self.api_keys:
            self.api_keys = self._load_api_keys_from_env()

    def _load_api_keys_from_env(self) -> Dict[str, str]:
        keys: Dict[str, str] = {}
        for key in (
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AZURE_API_KEY",
            "DEEPSEEK_API_KEY",
        ):
            value = os.getenv(key)
            if value:
                keys[key] = value
        return keys

    def _create_directories(self) -> None:
        for directory in (self.config_directory, self.logs_directory, self.cache_directory):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning("Could not create directory %s: %s", directory, exc)

    def to_dict(self, exclude_api_keys: bool = True) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if exclude_api_keys and key == "api_keys":
                continue
            if isinstance(value, Path):
                result[key] = str(value)
            elif isinstance(value, LogLevel):
                result[key] = value.value
            else:
                result[key] = value
        return result

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.warning("Unknown setting: %s", key)

    def apply_overrides(self, data: dict) -> None:
        """Apply nested ``.deile/settings.json`` overrides to flat fields.

        Walks ``_OVERRIDE_HANDLERS`` and pulls each known key out of *data*.
        Unknown keys are silently ignored (forward-compatible). Conversion
        errors leave the existing default in place plus a warning log.

        This method uses a two-pass approach: first validates all keys, then
        applies them — preventing partial application from crashing mid-way.
        """
        if not isinstance(data, dict) or not data:
            return
        pending: list = []
        for key_path, (field_name, converter) in _OVERRIDE_HANDLERS.items():
            found, raw = _resolve_dotted(data, key_path)
            if not found:
                continue
            try:
                pending.append((field_name, converter(raw)))
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "settings: cannot apply %s=%r (%s); skipping this key",
                    key_path, raw, exc,
                )
        for field_name, value in pending:
            if not hasattr(self, field_name):
                logger.error("settings: handler targets non-existent field %r", field_name)
                continue
            setattr(self, field_name, value)

    def save_to_file(self, file_path: Optional[str] = None) -> bool:
        """DEPRECATED. Use SettingsManager.set_setting() instead."""
        if file_path is None:
            logger.warning(
                "Settings.save_to_file() called without path is a no-op. "
                "Use SettingsManager.set_setting() to persist preferences."
            )
            return False
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
    def load_from_file(cls, file_path: Path) -> "Settings":
        """DEPRECATED (issue #111): read via SettingsManager + apply_overrides.

        Kept so existing callers passing an explicit path don't break;
        ``get_settings()`` no longer routes through this method.
        """
        try:
            with open(file_path, encoding="utf-8") as f:
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

    def get_api_key(self, provider: str) -> Optional[str]:
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
        return self.api_keys.get(key_name) if key_name else None

    def is_development(self) -> bool:
        return self.environment == "development"

    def is_production(self) -> bool:
        return self.environment == "production"

    def get_config_manager(self) -> Any:
        try:
            from .manager import get_config_manager

            return get_config_manager()
        except ImportError:
            logger.warning("ConfigManager not available, using fallback settings")
            return None

    def get_model_config(self) -> Dict[str, Any]:
        config_manager = self.get_config_manager()
        if config_manager:
            config = config_manager.get_config()
            return {
                "model_name": config.gemini.model_name,
                "temperature": config.gemini.generation_config.get("temperature", 0.1),
                "max_context_tokens": config.agent.max_context_tokens,
                "generation_config": config.gemini.generation_config,
                "tool_config": config.gemini.tool_config,
                "safety_settings": config.gemini.safety_settings,
            }
        return {"model_name": "gemini-1.5-pro-latest", "temperature": 0.1, "max_context_tokens": 8000}

    def validate(self) -> List[str]:
        issues: List[str] = []
        if not self.get_api_key(self.default_model_provider):
            issues.append(f"Missing API key for default provider: {self.default_model_provider}")
        if not self.working_directory.exists():
            issues.append(f"Working directory does not exist: {self.working_directory}")
        config_manager = self.get_config_manager()
        if config_manager:
            try:
                config = config_manager.get_config()
                issues.extend(config.validate())
            except Exception as exc:
                issues.append(f"ConfigManager validation failed: {exc}")
        if self.max_concurrent_requests <= 0:
            issues.append("max_concurrent_requests must be positive")
        return issues

    def __str__(self) -> str:
        return f"Settings(env={self.environment}, provider={self.default_model_provider})"

    def __repr__(self) -> str:
        return f"<Settings: {self.app_name} v{self.version}>"


# ---------------------------------------------------------------------------
# JSON loading helpers (no cross-package deps — keeps config/ self-contained)
# ---------------------------------------------------------------------------


def _load_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read settings file %s: %s", path, exc)
        return {}


_JSON_FIELD_MAP: Dict[str, str] = {
    # flat JSON key → Settings attribute
    "debug.enabled": "debug_enabled",
    "model.preferred": "preferred_model",
    "model.vision_model": "vision_model",
    "model.default_provider": "default_model_provider",
    "model.max_context_tokens": "max_context_tokens",
    "ui.streaming_enabled": "streaming_enabled",
    "ui.show_tool_details": "show_tool_details",
    "logging.level": "log_level",
    "logging.to_file": "log_to_file",
    "logging.max_size_mb": "log_file_max_size",
    "logging.backup_count": "log_file_backup_count",
    "caching.enabled": "enable_caching",
    "caching.ttl_seconds": "cache_ttl",
    "caching.parser_cache_enabled": "parser_cache_enabled",
    "caching.parser_cache_ttl": "parser_cache_ttl",
    "concurrency.max_concurrent_requests": "max_concurrent_requests",
    "concurrency.request_timeout": "request_timeout",
    "concurrency.max_tool_execution_time": "max_tool_execution_time",
    "file_safety.enabled": "enable_file_safety_checks",
    "file_safety.allowed_extensions": "allowed_file_extensions",
    "file_safety.blocked_directories": "blocked_directories",
    "file_safety.max_file_size_bytes": "max_file_size_bytes",
    "file_safety.allow_all_types": "allow_all_file_types",
    "file_safety.encoding_detection": "file_encoding_detection",
    "loop_guard.disabled": "loop_guard_disabled",
    "loop_guard.max_calls": "loop_guard_max_calls",
    "loop_guard.repeat_threshold": "loop_guard_repeat_threshold",
    "loop_guard.window_size": "loop_guard_window_size",
    "loop_guard.window_threshold": "loop_guard_window_threshold",
    "loop_guard.no_progress": "loop_guard_no_progress",
    "approval.auto": "bot_approval_auto",
    "deile_md.enabled": "deile_md_enabled",
    "deile_md.cwd_filename": "deile_md_cwd_filename",
    "deile_md.max_bytes": "deile_md_max_bytes",
    "skills_paths": "skills_paths",
    "profile.name": "profile_name",
    "environment": "environment",
    "pipeline.base_path": "pipeline_base_path",
    "pipeline.repo": "pipeline_repo",
    "pipeline.poll_interval": "pipeline_poll_interval",
    "pipeline.claude_timeout": "pipeline_claude_timeout",
    "pipeline.notify_user_id": "pipeline_notify_user_id",
    "pipeline.autostart": "pipeline_autostart",
    "cron.db_path": "cron_db_path",
    "cron.poll_interval": "cron_poll_interval",
}


def _apply_nested_dict(settings: "Settings", data: Dict[str, Any], prefix: str = "") -> None:
    """Recursively walk a nested dict and apply matching fields to settings."""
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if full_key in _JSON_FIELD_MAP:
            attr = _JSON_FIELD_MAP[full_key]
            _set_typed(settings, attr, value)
        elif isinstance(value, dict):
            _apply_nested_dict(settings, value, full_key)


def _set_typed(settings: "Settings", attr: str, value: Any) -> None:
    """Set a Settings attribute, coercing types as needed."""
    current = getattr(settings, attr, None)
    try:
        if isinstance(current, bool) and not isinstance(value, bool):
            value = str(value).lower() in {"1", "true", "yes", "on"}
        elif isinstance(current, int) and not isinstance(value, bool):
            value = int(value)
        elif isinstance(current, Path) or (current is None and attr in (
            "pipeline_base_path", "cron_db_path", "deile_md_user_path"
        )):
            value = Path(value) if value is not None else None
        elif isinstance(current, LogLevel):
            value = LogLevel(str(value).upper())
        setattr(settings, attr, value)
    except Exception as exc:
        logger.warning("Cannot apply setting %s=%r: %s", attr, value, exc)


def _apply_env_overrides(settings: "Settings") -> None:
    """Apply DEILE_* env vars on top of JSON settings, with deprecation warnings."""
    env = os.environ.get

    def _warn(var: str, json_key: str) -> None:
        logger.warning(
            "Env var %s is deprecated. Set '%s' in ~/.deile/settings.json instead.",
            var,
            json_key,
        )

    # debug
    raw = env("DEILE_DEBUG", "")
    if raw:
        _warn("DEILE_DEBUG", "debug.enabled")
        settings.debug_enabled = raw.lower() in {"1", "true", "yes", "on"}

    # model
    raw = env("DEILE_PREFERRED_MODEL")
    if raw:
        _warn("DEILE_PREFERRED_MODEL", "model.preferred")
        settings.preferred_model = raw

    raw = env("DEILE_VISION_MODEL")
    if raw:
        _warn("DEILE_VISION_MODEL", "model.vision_model")
        settings.vision_model = raw.strip()

    # approval
    raw = env("DEILE_BOT_APPROVAL_AUTO", "")
    if raw:
        _warn("DEILE_BOT_APPROVAL_AUTO", "approval.auto")
        settings.bot_approval_auto = raw.strip().lower() in {"1", "true", "yes", "on"}

    # loop_guard
    raw = env("DEILE_LOOP_GUARD_DISABLE", "")
    if raw:
        _warn("DEILE_LOOP_GUARD_DISABLE", "loop_guard.disabled")
        settings.loop_guard_disabled = raw.strip() in ("1", "true", "TRUE", "yes")

    for env_var, attr, cast, default in (
        ("DEILE_LOOP_GUARD_MAX_CALLS", "loop_guard_max_calls", int, 50),
        ("DEILE_LOOP_GUARD_REPEAT_THRESHOLD", "loop_guard_repeat_threshold", int, 3),
        ("DEILE_LOOP_GUARD_WINDOW_SIZE", "loop_guard_window_size", int, 5),
        ("DEILE_LOOP_GUARD_WINDOW_THRESHOLD", "loop_guard_window_threshold", int, 3),
        ("DEILE_LOOP_GUARD_NO_PROGRESS", "loop_guard_no_progress", int, 6),
    ):
        raw = env(env_var)
        if raw:
            json_key = _DEILE_DEPRECATED_ENV_VARS.get(env_var, env_var)
            _warn(env_var, json_key)
            try:
                setattr(settings, attr, max(1, cast(raw)))
            except ValueError:
                pass

    # pipeline
    raw = env("DEILE_PIPELINE_BASE_PATH")
    if raw:
        _warn("DEILE_PIPELINE_BASE_PATH", "pipeline.base_path")
        settings.pipeline_base_path = Path(raw).resolve()

    raw = env("DEILE_PIPELINE_REPO")
    if raw:
        _warn("DEILE_PIPELINE_REPO", "pipeline.repo")
        settings.pipeline_repo = raw

    raw = env("DEILE_PIPELINE_NOTIFY_USER_ID")
    if raw:
        _warn("DEILE_PIPELINE_NOTIFY_USER_ID", "pipeline.notify_user_id")
        settings.pipeline_notify_user_id = raw

    for env_var, attr, cast in (
        ("DEILE_PIPELINE_POLL_INTERVAL", "pipeline_poll_interval", int),
        ("DEILE_PIPELINE_CLAUDE_TIMEOUT", "pipeline_claude_timeout", int),
    ):
        raw = env(env_var)
        if raw:
            _warn(env_var, _DEILE_DEPRECATED_ENV_VARS.get(env_var, env_var))
            try:
                setattr(settings, attr, int(raw))
            except ValueError:
                pass

    raw = env("DEILE_PIPELINE_AUTOSTART")
    if raw:
        settings.pipeline_autostart = raw.lower().strip() in ("1", "true", "yes", "on")

    # cron
    raw = env("DEILE_CRON_DB_PATH")
    if raw:
        _warn("DEILE_CRON_DB_PATH", "cron.db_path")
        settings.cron_db_path = Path(raw).resolve()

    raw = env("DEILE_CRON_POLL_INTERVAL")
    if raw:
        _warn("DEILE_CRON_POLL_INTERVAL", "cron.poll_interval")
        try:
            settings.cron_poll_interval = int(raw)
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Layered loading helpers (issue #111)
# ---------------------------------------------------------------------------


def _load_layered_settings() -> "Settings":
    """Build a fresh ``Settings`` from defaults + ``.deile/settings.json`` layers.

    Order:
      1. Defaults from the dataclass.
      2. Apply ``~/.deile/settings.json`` (user) overrides.
      3. Apply ``<cwd>/.deile/settings.json`` (project) overrides on top.
      4. Apply DEILE_* env vars as deprecated fallback (win over JSON for
         backward compat).

    If neither layer exists but the legacy ``config/settings.json`` is
    present, fall back to it once with a deprecation warning.
    """
    settings = Settings()

    # Layer 1: global user preferences (~/.deile/settings.json)
    global_path = Path.home() / ".deile" / "settings.json"
    _apply_nested_dict(settings, _load_json_file(global_path))

    # Layer 2: project preferences (./.deile/settings.json — wins over global)
    project_path = Path.cwd() / ".deile" / "settings.json"
    _apply_nested_dict(settings, _load_json_file(project_path))

    # Legacy fallback: if neither new-layer file exists, honor config/settings.json
    if not global_path.exists() and not project_path.exists():
        legacy_path = Path.cwd() / "config" / "settings.json"
        if legacy_path.exists():
            logger.warning(
                "settings: loading legacy %s; migrate to ~/.deile/settings.json (issue #111)",
                legacy_path,
            )
            return Settings.load_from_file(legacy_path)

    # Layer 3: env vars as deprecated fallback (win over JSON for backward compat)
    _apply_env_overrides(settings)

    return settings


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_settings: Optional["Settings"] = None
_settings_lock = threading.Lock()


def get_settings() -> "Settings":
    """Retorna instância singleton das configurações.

    Reads ``.deile/settings.json`` (project > user > defaults). The legacy
    ``config/settings.json`` is honored as a one-shot fallback when neither
    new-layer file exists. Thread-safe via double-checked locking.
    """
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = _load_layered_settings()
    return _settings


def update_settings(**kwargs: Any) -> None:
    get_settings().update(**kwargs)


def reset_settings() -> None:
    global _settings
    with _settings_lock:
        _settings = None
