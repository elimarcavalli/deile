"""Tests for typed exceptions."""

from __future__ import annotations

import pytest

from deile_bot.foundation.exceptions import (AgentInvocationError,
                                             AgentInvocationTimeout,
                                             BotFoundationError,
                                             CapabilityNotSupported,
                                             ConversationStoreError, DLQError,
                                             FormatterError, IdentityError,
                                             PermissionDenied, ProviderError,
                                             RateLimited)


def test_all_subclass_base():
    for exc in [
        IdentityError,
        PermissionDenied,
        RateLimited,
        ConversationStoreError,
        AgentInvocationError,
        AgentInvocationTimeout,
        FormatterError,
        CapabilityNotSupported,
        ProviderError,
        DLQError,
    ]:
        assert issubclass(exc, BotFoundationError)


def test_timeout_is_invocation_error():
    assert issubclass(AgentInvocationTimeout, AgentInvocationError)


def test_context_attached():
    err = RateLimited("flooded", context={"reason": "user_burst", "user": "u1"})
    assert err.context["reason"] == "user_burst"
    assert err.context["user"] == "u1"


def test_no_context_default_empty():
    err = PermissionDenied("nope")
    assert err.context == {}
    assert "nope" in str(err)


def test_repr_includes_class_name():
    err = ProviderError("boom", context={"k": 1})
    r = repr(err)
    assert "ProviderError" in r
    assert "boom" in r


def test_raise_and_catch():
    with pytest.raises(BotFoundationError):
        raise RateLimited("hi")
