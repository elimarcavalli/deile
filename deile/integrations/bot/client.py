"""Lazy facade around `deilebot_client.BotControlClient`.

Why a facade and not a direct import:
- `deilebot_client` is an *optional* dependency (lives alongside the
  `deilebot` daemon in repo elimarcavalli/deilebot, exported as the
  thin HTTP client package). If missing, importing this module must NOT
  raise — it just reports BOT_CLIENT_AVAILABLE = False and the messaging
  tools auto-skip registration.
- We want a single shared client across tools (connection pool reuse).
- We want tests to be able to inject a fake client.
"""

from __future__ import annotations

import logging
from typing import Optional

from .config import BotIntegrationSettings, get_bot_settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import-time branch
    from deilebot_client import BotControlClient  # type: ignore
    from deilebot_client import BotControlSettings
    from deilebot_client.errors import (  # type: ignore  # noqa: F401
        BotClientAuthError, BotClientError, BotClientNotReady,
        BotClientRateLimited, BotClientTimeoutError, BotClientUpstreamError)

    BOT_CLIENT_AVAILABLE = True
except ImportError:
    BotControlClient = None  # type: ignore[assignment]
    BotControlSettings = None  # type: ignore[assignment]
    BotClientError = Exception  # type: ignore[misc]
    BotClientAuthError = BotClientError  # type: ignore[misc]
    BotClientNotReady = BotClientError  # type: ignore[misc]
    BotClientRateLimited = BotClientError  # type: ignore[misc]
    BotClientTimeoutError = BotClientError  # type: ignore[misc]
    BotClientUpstreamError = BotClientError  # type: ignore[misc]

    BOT_CLIENT_AVAILABLE = False


class BotClientFacade:
    """Singleton-friendly wrapper.

    The underlying `BotControlClient` is constructed lazily on first
    use and cached. Tests inject a custom client via
    `BotClientFacade.set_underlying(...)`.
    """

    _instance: Optional["BotClientFacade"] = None

    def __init__(self, settings: BotIntegrationSettings):
        self._settings = settings
        self._client = None

    @property
    def is_available(self) -> bool:
        return BOT_CLIENT_AVAILABLE and self._settings.is_configured

    @property
    def settings(self) -> BotIntegrationSettings:
        return self._settings

    def _ensure_client(self):
        if not BOT_CLIENT_AVAILABLE:
            raise RuntimeError(
                "deilebot_client is not installed; install with `pip install deile[bot]`"
            )
        if not self._settings.is_configured:
            raise RuntimeError(
                "deile→bot integration not configured "
                "(set DEILE_BOT_ENDPOINT and DEILE_BOT_AUTH_TOKEN, or BOT settings)"
            )
        if self._client is None:
            self._client = BotControlClient(  # type: ignore[misc]
                BotControlSettings(  # type: ignore[misc]
                    endpoint=self._settings.endpoint,
                    auth_token=self._settings.auth_token,
                    timeout_s=self._settings.timeout_s,
                    retry_attempts=self._settings.retry_attempts,
                )
            )
        return self._client

    def set_underlying(self, client) -> None:
        """Inject a custom client (testing/fakes)."""
        self._client = client

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "aclose"):
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover
                logger.exception("error closing BotControlClient")
        self._client = None

    # ---- Pass-through ops (kept thin so tools stay focused on policy) -------

    async def health(self):
        return await self._ensure_client().health()

    async def channel_post(self, **kwargs):
        return await self._ensure_client().discord_channel_post(**kwargs)

    async def dm_send(self, **kwargs):
        return await self._ensure_client().discord_dm_send(**kwargs)

    async def reaction_add(self, **kwargs):
        return await self._ensure_client().discord_reaction_add(**kwargs)

    async def thread_start(self, **kwargs):
        return await self._ensure_client().discord_thread_start(**kwargs)

    async def message_pin(self, **kwargs):
        return await self._ensure_client().discord_message_pin(**kwargs)

    async def role_mention(self, **kwargs):
        return await self._ensure_client().discord_role_mention(**kwargs)

    async def get_user(self, user_id: str):
        return await self._ensure_client().get_user_profile(user_id)

    async def whatsapp_send_template(self, **kwargs):
        return await self._ensure_client().whatsapp_send_template(**kwargs)


def get_bot_client(settings: Optional[BotIntegrationSettings] = None) -> BotClientFacade:
    """Process-wide singleton. Pass settings only to override (tests)."""
    if BotClientFacade._instance is None or settings is not None:
        BotClientFacade._instance = BotClientFacade(settings or get_bot_settings())
    return BotClientFacade._instance


def reset_bot_client() -> None:
    """Drop the cached singleton. Used by tests / hot-reload."""
    BotClientFacade._instance = None
