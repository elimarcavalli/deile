"""Shared fixtures for messaging-tool tests.

The tools talk to a `BotClientFacade`; rather than spinning a real
HTTP server for every test, we inject a `FakeBotClient` that records
calls and returns canned typed responses (the same Pydantic models the
real client returns).

Permission/audit/approval doubles are kept tiny so tests assert the
contract (was check_permission called?, was AuditEvent emitted?,
did the tool wait for approval?) instead of bytes-on-the-wire.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from deile.integrations.bot import reset_bot_client
from deile.integrations.bot.config import reset_bot_settings_cache
from deile.tools.base import ToolContext

# ---- canned response builders -----------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class _DummyResp:
    """Stand-in for the dataclass-like responses returned by the real client."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ---- fakes ------------------------------------------------------------------


class FakeBotClient:
    """In-memory stand-in for `BotClientFacade`.

    Captures every call into `self.calls` so assertions can inspect args.
    Each method returns a `_DummyResp` with the same attribute names as
    the real Pydantic models, so the production tools work unchanged.
    """

    def __init__(self, *, raise_on: Optional[Dict[str, Exception]] = None):
        self.calls: List[Dict[str, Any]] = []
        self.raise_on = raise_on or {}
        self._is_available = True

    @property
    def is_available(self) -> bool:
        return self._is_available

    def disable(self):
        self._is_available = False

    def _record(self, op: str, **kw):
        if op in self.raise_on:
            raise self.raise_on[op]
        self.calls.append({"op": op, **kw})

    async def aclose(self):
        return None

    async def health(self):
        self._record("health")
        return _DummyResp(ok=True, version="fake", providers=["discord"], is_ready=True)

    async def channel_post(self, *, channel_id, text, reply_to=None):
        self._record("channel_post", channel_id=channel_id, text=text, reply_to=reply_to)
        return _DummyResp(message_id="mid-" + channel_id, channel_id=channel_id, sent_at=_now())

    async def dm_send(self, *, text, user_id=None, bot_user_id=None):
        self._record("dm_send", user_id=user_id, bot_user_id=bot_user_id, text=text)
        return _DummyResp(message_id="dm-1", user_id=user_id or "resolved", sent_at=_now())

    async def reaction_add(self, *, channel_id, message_id, emoji):
        self._record("reaction_add", channel_id=channel_id, message_id=message_id, emoji=emoji)
        return _DummyResp(ok=True)

    async def thread_start(self, *, channel_id, name, parent_message_id=None):
        self._record("thread_start", channel_id=channel_id, name=name, parent_message_id=parent_message_id)
        return _DummyResp(thread_id="t-1", name=name)

    async def message_pin(self, *, channel_id, message_id):
        self._record("message_pin", channel_id=channel_id, message_id=message_id)
        return _DummyResp(ok=True)

    async def role_mention(self, *, channel_id, role_id, text=""):
        self._record("role_mention", channel_id=channel_id, role_id=role_id, text=text)
        return _DummyResp(message_id="rm-1")

    async def get_user(self, user_id):
        self._record("get_user", user_id=user_id)
        return _DummyResp(
            user_id=user_id,
            username="elimar.ciss",
            display_name="Elimar",
            avatar_url=None,
            is_bot=False,
        )


class FakePermissionManager:
    def __init__(self, allow: bool = True):
        self.allow = allow
        self.calls: List[Dict[str, Any]] = []

    def check_permission(self, *, tool_name, resource, action, context=None):
        self.calls.append(
            {"tool_name": tool_name, "resource": resource, "action": action, "context": context}
        )
        return self.allow


class FakeAuditLogger:
    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def log_event(self, **kw):
        self.events.append(kw)


class FakeApprovalSystem:
    """Approves/denies based on `decision`. Records the request payload."""

    def __init__(self, decision: bool = True):
        self.decision = decision
        self.requests: List[Dict[str, Any]] = []
        self._next_id = 0

    async def request_approval(self, **kw):
        self._next_id += 1
        self.requests.append(kw)
        return f"req-{self._next_id}"

    async def wait_for_approval(self, request_id):
        return self.decision


# ---- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch):
    """Reset cached singletons + scrub bot-related env vars so the test
    starts from a clean state regardless of the surrounding shell.

    Pytest tests of this module don't ever want to inherit a real env
    config (it would mask whatever the test is actually probing). Tests
    that *want* env vars set them explicitly via `monkeypatch.setenv`.
    """
    for key in (
        "DEILE_BOT_ENDPOINT",
        "DEILE_BOT_AUTH_TOKEN",
        "DEILE_BOT_TIMEOUT_S",
        "DEILE_BOT_DEFAULT_GUILD_ID",
        "DEILE_BOT_DISABLED",
        "DEILE_BOT_APPROVAL_AUTO",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_bot_settings_cache()
    reset_bot_client()
    yield
    reset_bot_settings_cache()
    reset_bot_client()


@pytest.fixture
def fake_client():
    return FakeBotClient()


@pytest.fixture
def fake_permission():
    return FakePermissionManager(allow=True)


@pytest.fixture
def fake_denied_permission():
    return FakePermissionManager(allow=False)


@pytest.fixture
def fake_audit():
    return FakeAuditLogger()


@pytest.fixture
def fake_approval_grant():
    return FakeApprovalSystem(decision=True)


@pytest.fixture
def fake_approval_deny():
    return FakeApprovalSystem(decision=False)


def make_context(
    *,
    args: Dict[str, Any],
    fake_client: Optional[FakeBotClient] = None,
    permission: Optional[FakePermissionManager] = None,
    audit: Optional[FakeAuditLogger] = None,
    approval: Optional[FakeApprovalSystem] = None,
) -> ToolContext:
    """Build a ToolContext with messaging-tool dependencies wired in via session_data."""
    session = {}
    if fake_client is not None:
        session["bot_client_facade"] = fake_client
    if permission is not None:
        session["permission_manager"] = permission
    if audit is not None:
        session["audit_logger"] = audit
    if approval is not None:
        session["approval_system"] = approval
    return ToolContext(user_input="", parsed_args=args, session_data=session)
