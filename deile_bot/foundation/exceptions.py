"""Typed exceptions for the bot foundation.

All public errors raised by foundation services subclass `BotFoundationError`
and accept an optional `context: Mapping[str, Any]` for structured payloads.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class BotFoundationError(Exception):
    """Base for all foundation errors."""

    def __init__(self, message: str, *, context: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.context: Mapping[str, Any] = dict(context) if context else {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.message!r}, context={dict(self.context)!r})"


class IdentityError(BotFoundationError):
    pass


class PermissionDenied(BotFoundationError):
    pass


class RateLimited(BotFoundationError):
    pass


class ConversationStoreError(BotFoundationError):
    pass


class AgentInvocationError(BotFoundationError):
    pass


class AgentInvocationTimeout(AgentInvocationError):
    pass


class FormatterError(BotFoundationError):
    pass


class CapabilityNotSupported(BotFoundationError):
    pass


class ProviderError(BotFoundationError):
    pass


class DLQError(BotFoundationError):
    pass
