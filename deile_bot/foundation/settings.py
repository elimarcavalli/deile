"""BotSettings — pydantic settings for the bot foundation.

Values may be loaded from env (prefix `DEILE_BOT_`), `.env`, or YAML.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]

    SettingsConfigDict = None  # type: ignore[assignment]


class FoundationSettings(BaseSettings):
    sqlite_path: Path = Path("./data/deile_bot.sqlite")
    sessions_sqlite_path: Path = Path("./data/deile_sessions.sqlite")
    data_retention_days: int = 90
    default_persona: str = "developer"
    intent_classifier: Literal[
        "heuristic", "llm", "always_respond_to_addressed", "always_respond"
    ] = "heuristic"
    rate_limit_user_burst: int = 5
    rate_limit_user_refill_per_minute: int = 30
    rate_limit_global_concurrent: int = 16
    agent_bridge_mode: Literal["in_process", "oneshot_subprocess"] = "in_process"
    agent_invocation_timeout_seconds: int = 120
    forced_model: Optional[str] = None
    default_model: Optional[str] = None
    audit_log_to_file: bool = True
    metrics_enabled: bool = True
    log_dir: Path = Path("./data/logs")
    raw_json_gzip_threshold_bytes: int = 4096
    session_strategy: Literal["per_user", "per_user_channel", "per_channel"] = "per_user"

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_"
            env_file = ".env"


class ProviderRegistrySettings(BaseSettings):
    enabled_providers: List[str] = Field(default_factory=list)

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_PROVIDERS_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_PROVIDERS_"
            env_file = ".env"


class PermissionRule(BaseModel):
    mode: Literal["owner_only", "allowlist", "wildcard"] = "wildcard"
    list: List[str] = Field(default_factory=list)


class PermissionsSettings(BaseModel):
    owners: List[str] = Field(default_factory=list)
    allowlist_invoke_agent: List[str] = Field(default_factory=lambda: ["*"])
    blocklist: List[str] = Field(default_factory=list)
    per_action: Dict[str, PermissionRule] = Field(default_factory=dict)


class PersonaRule(BaseModel):
    when: Dict[str, Any] = Field(default_factory=dict)
    use: str


class PersonaSettings(BaseModel):
    default: str = "developer"
    rules: List[PersonaRule] = Field(default_factory=list)


class BotSettings(BaseModel):
    """Top-level bag for foundation + cross-cutting settings.

    Each provider attaches its own settings (DiscordBotSettings, ...) on
    extra fields outside this base.
    """

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    foundation: FoundationSettings = Field(default_factory=FoundationSettings)
    providers: ProviderRegistrySettings = Field(default_factory=ProviderRegistrySettings)
    permissions: PermissionsSettings = Field(default_factory=PermissionsSettings)
    personas: PersonaSettings = Field(default_factory=PersonaSettings)


_YAML_PATH = Path("./config/deile_bot.yaml")


def _load_yaml() -> Dict[str, Any]:
    if not _YAML_PATH.exists():
        return {}
    try:
        return yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _env_prefix(cls) -> str:
    """Return the env_prefix for a BaseSettings subclass."""
    cfg = getattr(cls, "model_config", None)
    if cfg and isinstance(cfg, dict):
        return cfg.get("env_prefix", "")
    inner = getattr(cls, "Config", None)
    return getattr(inner, "env_prefix", "") if inner else ""


def _drop_env_overridden(data: Dict[str, Any], cls) -> Dict[str, Any]:
    """Remove keys that are already provided by environment variables.

    pydantic-settings v2 gives priority to init kwargs OVER env vars, which
    means passing YAML values as explicit kwargs would shadow any env var
    overrides (e.g. DEILE_BOT_INTENT_CLASSIFIER=always_respond would lose
    to the YAML value).  Dropping keys that have a matching env var restores
    the expected env > YAML precedence.
    """
    import os

    prefix = _env_prefix(cls).upper()
    return {
        k: v
        for k, v in data.items()
        if not os.environ.get(f"{prefix}{k.upper()}")
    }


def _build_settings() -> BotSettings:
    yaml_data = _load_yaml()
    foundation_data = _drop_env_overridden(yaml_data.get("foundation", {}), FoundationSettings)
    providers_data = yaml_data.get("providers", {})
    permissions_data = yaml_data.get("permissions", {})
    personas_data = yaml_data.get("personas", {})
    return BotSettings(
        foundation=FoundationSettings(**foundation_data),
        providers=ProviderRegistrySettings(**providers_data),
        permissions=PermissionsSettings(**permissions_data),
        personas=PersonaSettings(**personas_data),
    )


@lru_cache(maxsize=1)
def get_bot_settings() -> BotSettings:
    """Return singleton BotSettings (env > .env > YAML > defaults)."""
    return _build_settings()


def reset_bot_settings_cache() -> None:
    """Reset the singleton cache. Useful in tests."""
    get_bot_settings.cache_clear()
