"""Unit tests for ``deile.tools.dispatch_deile_task``.

Cover Pydantic payload validation, anti-loop cooldown rollback on
pre-network failures, the TTL-based cleanup that bounds the
``_LAST_DISPATCH`` cache size under sustained traffic, and the recent-
history forwarding: the ingress pipeline injects
``bot_context.recent_history`` on the bot-mediated path and the tool
forwards it to the worker, while the ``/deile`` passthrough (whose
ToolContext carries no ``recent_history``) stays one-shot.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from deile.infrastructure.deile_worker_client import (
    DeileWorkerClient,
    WorkerDispatchError,
)
from deile.tools.base import ToolContext
from deile.tools.dispatch_deile_task import DispatchDeileTaskTool


class _FakeResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _FakeAsyncClient:
    """Captures the POST payload; returns a canned worker success.

    The history-forwarding tests drive the REAL ``DeileWorkerClient`` (no
    injected stub) so the captured ``last_payload`` is the actual wire body
    — i.e. after ``DispatchPayload`` validation and
    ``model_dump(exclude_none=True)``. That is what guards that ``history``
    survives the Pydantic round-trip instead of being silently dropped.
    """

    last_payload = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, headers=None):
        _FakeAsyncClient.last_payload = json
        return _FakeResponse(
            {"ok": True, "task_id": "t-1", "elapsed_s": 1.0, "files": []}
        )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Isolate class-level state between tests. ``_CHANNEL_LOCKS`` is
    # cleared too because cached ``asyncio.Lock`` instances bind to the
    # event loop on first acquire — keeping them across tests can entangle
    # distinct test loops. The token env + httpx patch let the history
    # tests drive the REAL client without touching network or secrets; the
    # token must be >=16 chars to pass the adapter's bearer charset check.
    httpx = pytest.importorskip("httpx")
    monkeypatch.setenv("DEILE_WORKER_BEARER_TOKEN", "test-token-0123456789abcdef")
    DispatchDeileTaskTool._LAST_DISPATCH.clear()
    DispatchDeileTaskTool._CHANNEL_LOCKS.clear()
    _FakeAsyncClient.last_payload = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    yield
    DispatchDeileTaskTool._LAST_DISPATCH.clear()
    DispatchDeileTaskTool._CHANNEL_LOCKS.clear()


def _ctx(**args) -> ToolContext:
    return ToolContext(
        user_input="",
        parsed_args=args,
        session_data={"bot_context": {}},
    )


def _code(result) -> str:
    return result.metadata.get("error_code", "")


class _StubClient(DeileWorkerClient):
    def __init__(self, *, raises=None, returns=None):
        self._raises = raises
        self._returns = returns or {"ok": True, "task_id": "T", "files": []}
        self.calls = []

    async def dispatch(self, payload, *, wait):
        self.calls.append((payload, wait))
        if self._raises is not None:
            raise self._raises
        return self._returns


async def test_brief_required_returns_bad_request():
    tool = DispatchDeileTaskTool(worker_client=_StubClient())
    result = await tool.execute(_ctx(channel_id="abc"))
    assert not result.is_success
    assert _code(result) == "BAD_REQUEST"


async def test_channel_id_required_returns_bad_request():
    tool = DispatchDeileTaskTool(worker_client=_StubClient())
    result = await tool.execute(_ctx(brief="do thing"))
    assert not result.is_success
    assert _code(result) == "BAD_REQUEST"


async def test_invalid_persona_pydantic_rejection():
    tool = DispatchDeileTaskTool(worker_client=_StubClient())
    result = await tool.execute(
        _ctx(brief="do thing", channel_id="abc", persona="hacker")
    )
    assert not result.is_success
    assert _code(result) == "BAD_REQUEST"
    # No cooldown consumed by the rejected request.
    assert "abc" not in DispatchDeileTaskTool._LAST_DISPATCH


async def test_dispatch_success_records_cooldown():
    stub = _StubClient()
    tool = DispatchDeileTaskTool(worker_client=stub)
    result = await tool.execute(_ctx(brief="b", channel_id="c"))
    assert result.is_success
    assert "c" in DispatchDeileTaskTool._LAST_DISPATCH


async def test_cooldown_blocks_immediate_second_dispatch():
    stub = _StubClient()
    tool = DispatchDeileTaskTool(worker_client=stub)
    await tool.execute(_ctx(brief="b", channel_id="c"))
    result = await tool.execute(_ctx(brief="b2", channel_id="c"))
    assert not result.is_success
    assert _code(result) == "DISPATCH_COOLDOWN"
    assert len(stub.calls) == 1  # second never reached the client


async def test_cooldown_rollback_on_auth_missing():
    stub = _StubClient(
        raises=WorkerDispatchError("no token", error_code="WORKER_AUTH_MISSING")
    )
    tool = DispatchDeileTaskTool(worker_client=stub)
    result = await tool.execute(_ctx(brief="b", channel_id="c"))
    assert not result.is_success
    assert _code(result) == "WORKER_AUTH_MISSING"
    # Pre-network failure: cooldown rolled back so user can fix env and retry.
    assert "c" not in DispatchDeileTaskTool._LAST_DISPATCH


async def test_cooldown_rollback_on_transport_missing():
    stub = _StubClient(
        raises=WorkerDispatchError("no httpx", error_code="WORKER_TRANSPORT_MISSING")
    )
    tool = DispatchDeileTaskTool(worker_client=stub)
    result = await tool.execute(_ctx(brief="b", channel_id="c"))
    assert _code(result) == "WORKER_TRANSPORT_MISSING"
    assert "c" not in DispatchDeileTaskTool._LAST_DISPATCH


async def test_cooldown_rollback_on_auth_malformed():
    stub = _StubClient(
        raises=WorkerDispatchError("bad chars", error_code="WORKER_AUTH_MALFORMED")
    )
    tool = DispatchDeileTaskTool(worker_client=stub)
    result = await tool.execute(_ctx(brief="b", channel_id="c"))
    assert _code(result) == "WORKER_AUTH_MALFORMED"
    assert "c" not in DispatchDeileTaskTool._LAST_DISPATCH


async def test_cooldown_NOT_rolled_back_on_network_failure():
    """Genuine worker reach attempts keep the cooldown to prevent flooding."""
    stub = _StubClient(
        raises=WorkerDispatchError("timeout", error_code="WORKER_TIMEOUT")
    )
    tool = DispatchDeileTaskTool(worker_client=stub)
    result = await tool.execute(_ctx(brief="b", channel_id="c"))
    assert _code(result) == "WORKER_TIMEOUT"
    # Network actually attempted — keep the cooldown.
    assert "c" in DispatchDeileTaskTool._LAST_DISPATCH


async def test_prune_expired_dispatch_entries():
    # Inject a stale entry then trigger a fresh dispatch that should
    # prune it.
    now = time.monotonic()
    cutoff = (
        DispatchDeileTaskTool._DISPATCH_COOLDOWN_S
        * DispatchDeileTaskTool._CLEANUP_FACTOR
    )
    DispatchDeileTaskTool._LAST_DISPATCH["stale"] = now - cutoff - 1.0
    DispatchDeileTaskTool._LAST_DISPATCH["fresh"] = now - 1.0
    DispatchDeileTaskTool._prune_expired_dispatch_entries(now)
    assert "stale" not in DispatchDeileTaskTool._LAST_DISPATCH
    assert "fresh" in DispatchDeileTaskTool._LAST_DISPATCH


async def test_payload_propagation_includes_user_message_id():
    stub = _StubClient()
    tool = DispatchDeileTaskTool(worker_client=stub)
    await tool.execute(_ctx(brief="b", channel_id="c", user_message_id="msg-99"))
    assert stub.calls[0][0]["user_message_id"] == "msg-99"


# ----- TOCTOU regression: concurrent dispatch must serialize -----


class _SlowStubClient(DeileWorkerClient):
    """Stub that blocks on an event so the test can park the first call
    inside the worker dispatch while a second call races into ``execute()``.
    """

    def __init__(self) -> None:
        self.calls = []
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def dispatch(self, payload, *, wait):
        self.calls.append((payload, wait))
        self.entered.set()
        await self.release.wait()
        return {"ok": True, "task_id": "T", "files": []}


async def test_concurrent_dispatch_same_channel_serializes_via_lock():
    """Pins the per-channel ``asyncio.Lock`` TOCTOU fix.

    Two coroutines arriving on the same ``channel_id`` before the first
    has written its cooldown timestamp must NOT both reach the worker —
    exactly one is dispatched, the other gets ``DISPATCH_COOLDOWN``.
    Without the lock, both observe ``last=None`` and both spawn workers.
    """
    stub = _SlowStubClient()
    tool = DispatchDeileTaskTool(worker_client=stub)

    first = asyncio.create_task(tool.execute(_ctx(brief="b1", channel_id="c")))
    # Wait until the first call is parked inside the stub's dispatch —
    # the cooldown timestamp has already been written under the lock,
    # but the call hasn't returned. This is exactly the window the race
    # would exploit if the check+write were not atomic.
    await stub.entered.wait()

    second = asyncio.create_task(tool.execute(_ctx(brief="b2", channel_id="c")))
    # Let both tasks settle. The second one returns synchronously with
    # the cooldown error once the lock briefly hands off.
    second_result = await second
    stub.release.set()
    first_result = await first

    assert first_result.is_success
    assert not second_result.is_success
    assert _code(second_result) == "DISPATCH_COOLDOWN"
    assert len(stub.calls) == 1


async def test_concurrent_dispatch_distinct_channels_do_not_block():
    """Distinct ``channel_id`` values get distinct locks — no contention."""
    stub_a = _SlowStubClient()
    stub_b = _SlowStubClient()
    tool_a = DispatchDeileTaskTool(worker_client=stub_a)
    tool_b = DispatchDeileTaskTool(worker_client=stub_b)

    task_a = asyncio.create_task(tool_a.execute(_ctx(brief="b", channel_id="A")))
    task_b = asyncio.create_task(tool_b.execute(_ctx(brief="b", channel_id="B")))
    # Both should be able to enter the worker concurrently — neither
    # blocks the other.
    await asyncio.gather(stub_a.entered.wait(), stub_b.entered.wait())
    stub_a.release.set()
    stub_b.release.set()
    res_a, res_b = await asyncio.gather(task_a, task_b)
    assert res_a.is_success and res_b.is_success


async def test_prune_drops_orphan_unlocked_channel_lock():
    """``_prune_expired_dispatch_entries`` reaps locks for channels no
    longer tracked in ``_LAST_DISPATCH`` IFF the lock is not currently
    held. Bounds both class-level dicts without racing against an
    in-flight dispatch.
    """
    # ``orphan`` has no ``_LAST_DISPATCH`` entry and its lock is
    # unlocked — must be pruned.
    DispatchDeileTaskTool._CHANNEL_LOCKS["orphan"] = asyncio.Lock()
    # ``held`` is acquired while we prune — must survive.
    held = asyncio.Lock()
    await held.acquire()
    try:
        DispatchDeileTaskTool._CHANNEL_LOCKS["held"] = held
        DispatchDeileTaskTool._prune_expired_dispatch_entries(time.monotonic())
        assert "orphan" not in DispatchDeileTaskTool._CHANNEL_LOCKS
        assert "held" in DispatchDeileTaskTool._CHANNEL_LOCKS
    finally:
        held.release()


# ----- recent-history forwarding (bot-mediated path vs /deile passthrough) -----


async def test_forwards_recent_history_from_bot_context():
    # Drives the REAL client so the assertion runs on the wire body,
    # proving ``history`` survives DispatchPayload validation + model_dump.
    tool = DispatchDeileTaskTool()
    ctx = ToolContext(
        user_input="faz X",
        parsed_args={"brief": "faz X", "channel_id": "chan-a"},
        session_data={"bot_context": {"recent_history": "[user] oi\n[deile] olá"}},
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert _FakeAsyncClient.last_payload["history"] == "[user] oi\n[deile] olá"


async def test_omits_history_when_bot_context_has_none():
    tool = DispatchDeileTaskTool()
    ctx = ToolContext(
        user_input="faz Y",
        parsed_args={"brief": "faz Y", "channel_id": "chan-b"},
        session_data={"bot_context": {}},
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert "history" not in _FakeAsyncClient.last_payload


async def test_omits_history_on_passthrough_without_bot_context():
    # The /deile passthrough builds a ToolContext without recent_history,
    # so the worker stays one-shot there.
    tool = DispatchDeileTaskTool()
    ctx = ToolContext(
        user_input="faz Z",
        parsed_args={"brief": "faz Z", "channel_id": "chan-c"},
        session_data={},
    )
    result = await tool.execute(ctx)
    assert result.is_success
    assert "history" not in _FakeAsyncClient.last_payload
