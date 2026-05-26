"""Regression tests for the google-genai aclose() defensive monkey-patch.

google-genai 1.47.0 has a bug in ``BaseApiClient.aclose()``: it dereferences
``self._async_httpx_client`` (and ``self._aiohttp_session``) unconditionally,
but those are LAZY-initialized. When the client is GC'd before any async
request fires, ``aclose()`` raises ``AttributeError`` — and because it runs
as a fire-and-forget Task scheduled from ``__del__``, the exception is never
retrieved and asyncio logs the noisy stack trace twice at every worker boot.

The patch lives in ``deile/core/models/gemini_provider.py`` and:

1. Wraps ``BaseApiClient.aclose`` once (idempotent guard).
2. Catches AttributeError with ``_async_httpx_client`` or ``_aiohttp_session``
   in the message → silent no-op.
3. Re-raises any OTHER AttributeError (don't mask unrelated bugs).
4. Preserves the original close path when attributes ARE initialized.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai._api_client import BaseApiClient

# Trigger the patch install (idempotent — already done if gemini_provider was
# imported elsewhere).
import deile.core.models.gemini_provider  # noqa: F401


def test_aclose_is_guarded():
    """The wrapper marker should be present on the bound method."""
    assert getattr(BaseApiClient.aclose, "_deile_guarded", False) is True


def test_install_is_idempotent():
    """Calling the installer twice must not stack wrappers."""
    from deile.core.models.gemini_provider import _install_genai_aclose_guard

    first = BaseApiClient.aclose
    _install_genai_aclose_guard()
    second = BaseApiClient.aclose
    assert first is second


async def test_missing_async_httpx_client_is_silently_swallowed():
    """The original SDK bug — never-initialized async client — must no-op."""
    instance = SimpleNamespace()
    # Bind the patched method to the bare instance — no `_async_httpx_client`.
    # If the patch missed, this raises AttributeError; with the patch, it
    # returns None silently.
    result = await BaseApiClient.aclose(instance)
    assert result is None


async def test_missing_aiohttp_session_is_silently_swallowed():
    """Same as above but for the second lazy attribute the SDK touches."""

    class _Closeable:
        def __init__(self):
            self.aclose = AsyncMock(return_value=None)

    instance = SimpleNamespace(_async_httpx_client=_Closeable())
    # _aiohttp_session is missing — the original aclose would fail here.
    # The guard catches it as the same SDK lazy-attr family.
    result = await BaseApiClient.aclose(instance)
    assert result is None
    instance._async_httpx_client.aclose.assert_awaited_once()


async def test_unrelated_attribute_error_is_re_raised():
    """Don't mask unrelated bugs — only the known SDK lazy-attr names are quiet."""

    class _Bomb:
        async def aclose(self):
            raise AttributeError("some_completely_unrelated_attr is gone")

    instance = SimpleNamespace(
        _async_httpx_client=_Bomb(),
        _aiohttp_session=None,
    )
    with pytest.raises(AttributeError, match="some_completely_unrelated_attr"):
        await BaseApiClient.aclose(instance)


async def test_happy_path_with_initialized_clients():
    """When both lazy attrs are real, aclose() calls through normally."""
    httpx_client = SimpleNamespace(aclose=AsyncMock(return_value=None))
    aiohttp_session = SimpleNamespace(close=AsyncMock(return_value=None))
    instance = SimpleNamespace(
        _async_httpx_client=httpx_client,
        _aiohttp_session=aiohttp_session,
    )
    await BaseApiClient.aclose(instance)
    httpx_client.aclose.assert_awaited_once()
    aiohttp_session.close.assert_awaited_once()


async def test_patch_silences_the_real_scenario():
    """End-to-end: a real BaseApiClient that never opened an async session.

    This is exactly what happens in the worker pod at boot: GeminiProvider
    instantiates a client, never sends a request, then GC kicks in and
    schedules aclose(). With the guard, aclose() returns cleanly; without it,
    asyncio would log a 'Task exception was never retrieved' AttributeError.
    """
    # We construct an instance bypassing __init__ so no httpx_client exists.
    client = object.__new__(BaseApiClient)
    # Schedule aclose() exactly like __del__ does — fire-and-forget — and
    # await it deterministically here so the test verifies the no-raise path.
    task = asyncio.ensure_future(client.aclose())
    await task
    # No exception → patch held the contract.
    assert task.exception() is None
