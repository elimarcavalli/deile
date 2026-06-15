"""Regression tests for the google-genai aclose() defensive monkey-patch.

google-genai's ``BaseApiClient.aclose()`` dereferences lazily-initialized
internal attributes. When the client is GC'd before any async request fires,
those attributes never existed, so ``aclose()`` raises ``AttributeError`` — and
because it runs as a fire-and-forget Task scheduled from ``__del__``, the
exception is never retrieved and asyncio logs the noisy stack trace at every
worker boot.

The exact set of internals aclose() touches DRIFTS across SDK versions
(1.47.0: ``_async_httpx_client`` / ``_aiohttp_session``; 2.x adds
``_http_options``), so the guard in ``deile/core/models/gemini_provider.py``
keys off the SIGNATURE — a missing underscore-prefixed (internal) attribute on
the client — not a hardcoded name list. It:

1. Wraps ``BaseApiClient.aclose`` once (idempotent guard).
2. Swallows AttributeError whose message names a missing INTERNAL attribute
   (``_is_genai_lazy_attr_error``) → silent no-op.
3. Re-raises any OTHER AttributeError (missing public attr, unrelated message)
   so genuine bugs stay loud.

These tests exercise the decision predicate directly (version-independent) plus
one end-to-end test against a real ``BaseApiClient`` — instead of hand-building
SimpleNamespace stand-ins that mirror a specific SDK version's aclose() body.
"""

from __future__ import annotations

import asyncio

import pytest
from google.genai._api_client import BaseApiClient

# Trigger the patch install (idempotent — already done if gemini_provider was
# imported elsewhere).
import deile.core.models.gemini_provider  # noqa: F401
from deile.core.models.gemini_provider import _is_genai_lazy_attr_error


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


@pytest.mark.parametrize(
    "attr",
    ["_async_httpx_client", "_aiohttp_session", "_http_options", "_some_future_attr"],
)
def test_lazy_attr_error_swallowed_for_internal_names(attr):
    """Any missing INTERNAL (underscore) attribute is the lazy-init family.

    Covers the historical names AND a hypothetical future one — proving the
    guard survives SDK version drift without a code change.
    """
    exc = AttributeError(f"'BaseApiClient' object has no attribute '{attr}'")
    assert _is_genai_lazy_attr_error(exc) is True


@pytest.mark.parametrize(
    "message",
    [
        "some_completely_unrelated_attr is gone",  # custom message, not the CPython shape
        "'BaseApiClient' object has no attribute 'public_thing'",  # public attr = real bug
        "'GeminiProvider' object has no attribute 'model'",  # unrelated public attr
    ],
)
def test_unrelated_attribute_error_is_not_swallowed(message):
    """Don't mask unrelated bugs — only missing INTERNAL attrs are quiet."""
    assert _is_genai_lazy_attr_error(AttributeError(message)) is False


async def test_patch_silences_the_real_scenario():
    """End-to-end: a real BaseApiClient that never opened an async session.

    This is exactly what happens in the worker pod at boot: GeminiProvider
    instantiates a client, never sends a request, then GC kicks in and
    schedules aclose(). With the guard, aclose() returns cleanly; without it,
    asyncio would log a 'Task exception was never retrieved' AttributeError.

    Constructed via ``object.__new__`` so NONE of the lazy internals exist —
    whatever attribute the installed SDK's aclose() reaches for first, the
    guard must absorb it.
    """
    client = object.__new__(BaseApiClient)
    # Schedule aclose() exactly like __del__ does — fire-and-forget — and
    # await it deterministically here so the test verifies the no-raise path.
    task = asyncio.ensure_future(client.aclose())
    await task
    # No exception → patch held the contract.
    assert task.exception() is None
