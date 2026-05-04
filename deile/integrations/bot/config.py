"""Settings for the deile→deilebot integration.

Loaded from env (DEILE_BOT_*) or .env. The token is treated as a secret
— `__repr__` masks it.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _HAVE = True
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]
    SettingsConfigDict = None  # type: ignore[assignment]
    _HAVE = False


class BotIntegrationSettings(BaseSettings):
    """Configuration for the deile→deilebot daemon link.

    `endpoint` and `auth_token` are required for messaging tools to
    register. `default_guild_id` is informational (used as a hint by
    some tools when context is missing).
    """

    endpoint: str = ""
    auth_token: str = ""
    default_guild_id: Optional[str] = None
    timeout_s: float = 10.0
    retry_attempts: int = 3
    # When True, even if the client package and endpoint are present,
    # tools refuse to register. Safety knob for ops.
    disabled: bool = False

    if _HAVE:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_"
            env_file = ".env"

    @property
    def is_configured(self) -> bool:
        """True iff the integration has the minimum needed to talk to the daemon."""
        return bool(self.endpoint) and bool(self.auth_token) and not self.disabled

    def __repr__(self) -> str:  # pragma: no cover
        token = "<set>" if self.auth_token else "<unset>"
        return (
            f"BotIntegrationSettings(endpoint={self.endpoint!r}, auth_token={token}, "
            f"default_guild_id={self.default_guild_id!r}, disabled={self.disabled})"
        )


@lru_cache(maxsize=1)
def get_bot_settings() -> BotIntegrationSettings:
    """Singleton accessor — populated from env on first call."""
    return BotIntegrationSettings()


def reset_bot_settings_cache() -> None:
    """Clear the cache — used by tests that monkey-patch env vars.

    Defensive against tests that monkeypatch `get_bot_settings` with a
    plain function (no cache_clear): we just no-op in that case.
    """
    clear = getattr(get_bot_settings, "cache_clear", None)
    if callable(clear):
        clear()
