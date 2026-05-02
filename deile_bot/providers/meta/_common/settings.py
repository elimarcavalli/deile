"""Meta common settings (Page access token, verify token)."""

from __future__ import annotations

from pydantic import SecretStr

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # pragma: no cover
    from pydantic import BaseSettings  # type: ignore[attr-defined]

    SettingsConfigDict = None  # type: ignore[assignment]


class MetaCommonSettings(BaseSettings):
    page_access_token: SecretStr = SecretStr("")
    page_id: str = ""
    ig_business_id: str = ""
    verify_token: SecretStr = SecretStr("")
    api_version: str = "v22.0"

    if SettingsConfigDict is not None:
        model_config = SettingsConfigDict(
            env_prefix="DEILE_BOT_META_",
            env_file=".env",
            extra="ignore",
        )
    else:  # pragma: no cover
        class Config:
            env_prefix = "DEILE_BOT_META_"
            env_file = ".env"
