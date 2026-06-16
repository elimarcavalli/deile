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

import asyncio
import logging
import random
import time
from typing import Optional

from .config import BotIntegrationSettings, get_bot_settings

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import-time branch
    from deilebot_client import BotControlClient  # type: ignore
    from deilebot_client import (
        BotControlSettings,
    )
    from deilebot_client.errors import (  # type: ignore  # noqa: F401
        BotClientAuthError,
        BotClientError,
        BotClientNotReady,
        BotClientRateLimited,
        BotClientTimeoutError,
        BotClientUpstreamError,
    )

    BOT_CLIENT_AVAILABLE = True
except ImportError:
    BotControlClient = None  # type: ignore[assignment]
    BotControlSettings = None  # type: ignore[assignment]

    class BotClientError(Exception):  # type: ignore[no-redef]
        def __init__(self, message: str = "", code: str = "", **kwargs):
            super().__init__(message)
            self.code = code

    class BotClientAuthError(BotClientError):  # type: ignore[no-redef]
        pass

    class BotClientNotReady(BotClientError):  # type: ignore[no-redef]
        pass

    class BotClientRateLimited(BotClientError):  # type: ignore[no-redef]
        pass

    class BotClientTimeoutError(BotClientError):  # type: ignore[no-redef]
        pass

    class BotClientUpstreamError(BotClientError):  # type: ignore[no-redef]
        pass

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

    # ---- retry with exponential backoff -----------------------------------

    # Mapping of facade method -> underlying client method
    _MESSAGING_METHODS: dict[str, str] = {
        "channel_post": "discord_channel_post",
        "dm_send": "discord_dm_send",
        "reaction_add": "discord_reaction_add",
        "thread_start": "discord_thread_start",
        "message_pin": "discord_message_pin",
        "message_edit": "discord_message_edit",
        "role_mention": "discord_role_mention",
        "whatsapp_send_template": "whatsapp_send_template",
    }

    async def _retry_messaging(self, op_name: str, **kwargs):
        """Execute a messaging operation with exponential backoff retry.

        Only retries on transient errors (timeout, rate-limit, upstream 5xx/429).
        Non-transient errors (auth, not-ready, upstream 4xx) fail immediately.
        """
        max_attempts = self._settings.retry_attempts
        timeout_s = self._settings.timeout_s
        deadline = time.monotonic() + (timeout_s * max_attempts)
        last_exc: Optional[Exception] = None
        base_delay = 1.0
        client_method_name = self._MESSAGING_METHODS[op_name]

        for attempt in range(1, max_attempts + 1):
            # Budget check before the attempt
            if time.monotonic() >= deadline:
                if last_exc is not None:
                    raise last_exc
                raise BotClientTimeoutError(
                    f"{op_name}: retry budget exhausted "
                    f"(timeout_s={timeout_s}, retry_attempts={max_attempts})"
                )

            try:
                client = self._ensure_client()
                method = getattr(client, client_method_name)
                return await method(**kwargs)
            except (
                BotClientTimeoutError,
                BotClientRateLimited,
                BotClientUpstreamError,
            ) as exc:
                last_exc = exc

                # BotClientUpstreamError with non-transient status → no retry
                if isinstance(exc, BotClientUpstreamError):
                    status = getattr(exc, "status_code", None)
                    if status is not None and not (
                        500 <= status < 600 or status == 429
                    ):
                        raise

                # Exhausted retries → propagate last exception
                if attempt >= max_attempts:
                    raise

                # Calculate delay with exponential backoff + jitter
                delay = base_delay * (2 ** (attempt - 1))
                jitter = random.uniform(0, delay * 0.2)
                total_delay = delay + jitter

                # Budget check before sleeping
                if time.monotonic() + total_delay >= deadline:
                    raise last_exc

                logger.warning(
                    "Bot messaging retry: %s attempt %d/%d after %.1fs — %s: %s",
                    op_name,
                    attempt,
                    max_attempts,
                    total_delay,
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(total_delay)

        # Should be unreachable, but defensively propagate the last error
        if last_exc is not None:
            raise last_exc

    # ---- non-messaging ops (no retry) -------------------------------------

    async def health(self):
        return await self._ensure_client().health()

    async def get_user(self, user_id: str):
        return await self._ensure_client().get_user_profile(user_id)

    # ---- messaging ops (with retry) ---------------------------------------

    async def channel_post(self, **kwargs):
        return await self._retry_messaging("channel_post", **kwargs)

    async def dm_send(self, **kwargs):
        return await self._retry_messaging("dm_send", **kwargs)

    async def reaction_add(self, **kwargs):
        return await self._retry_messaging("reaction_add", **kwargs)

    async def thread_start(self, **kwargs):
        return await self._retry_messaging("thread_start", **kwargs)

    async def message_pin(self, **kwargs):
        return await self._retry_messaging("message_pin", **kwargs)

    async def message_edit(self, **kwargs):
        return await self._retry_messaging("message_edit", **kwargs)

    async def role_mention(self, **kwargs):
        return await self._retry_messaging("role_mention", **kwargs)

    async def whatsapp_send_template(self, **kwargs):
        return await self._retry_messaging("whatsapp_send_template", **kwargs)


def get_bot_client(
    settings: Optional[BotIntegrationSettings] = None,
) -> BotClientFacade:
    """Process-wide singleton. Pass settings only to override (tests)."""
    if BotClientFacade._instance is None or settings is not None:
        BotClientFacade._instance = BotClientFacade(settings or get_bot_settings())
    return BotClientFacade._instance


def reset_bot_client() -> None:
    """Drop the cached singleton. Used by tests / hot-reload."""
    BotClientFacade._instance = None
