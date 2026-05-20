"""Unit tests for ``deile.tools.dispatch_deile_task``.

Cover Pydantic payload validation, anti-loop cooldown rollback on
pre-network failures, and the TTL-based cleanup that bounds the
``_LAST_DISPATCH`` cache size under sustained traffic.
"""
from __future__ import annotations

import time

import pytest

from deile.infrastructure.deile_worker_client import (DeileWorkerClient,
                                                      WorkerDispatchError)
from deile.tools.base import ToolContext
from deile.tools.dispatch_deile_task import DispatchDeileTaskTool


@pytest.fixture(autouse=True)
def _clear_last_dispatch():
    DispatchDeileTaskTool._LAST_DISPATCH.clear()
    yield
    DispatchDeileTaskTool._LAST_DISPATCH.clear()


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
        raises=WorkerDispatchError(
            "no token", error_code="WORKER_AUTH_MISSING"
        )
    )
    tool = DispatchDeileTaskTool(worker_client=stub)
    result = await tool.execute(_ctx(brief="b", channel_id="c"))
    assert not result.is_success
    assert _code(result) == "WORKER_AUTH_MISSING"
    # Pre-network failure: cooldown rolled back so user can fix env and retry.
    assert "c" not in DispatchDeileTaskTool._LAST_DISPATCH


async def test_cooldown_rollback_on_transport_missing():
    stub = _StubClient(
        raises=WorkerDispatchError(
            "no httpx", error_code="WORKER_TRANSPORT_MISSING"
        )
    )
    tool = DispatchDeileTaskTool(worker_client=stub)
    result = await tool.execute(_ctx(brief="b", channel_id="c"))
    assert _code(result) == "WORKER_TRANSPORT_MISSING"
    assert "c" not in DispatchDeileTaskTool._LAST_DISPATCH


async def test_cooldown_rollback_on_auth_malformed():
    stub = _StubClient(
        raises=WorkerDispatchError(
            "bad chars", error_code="WORKER_AUTH_MALFORMED"
        )
    )
    tool = DispatchDeileTaskTool(worker_client=stub)
    result = await tool.execute(_ctx(brief="b", channel_id="c"))
    assert _code(result) == "WORKER_AUTH_MALFORMED"
    assert "c" not in DispatchDeileTaskTool._LAST_DISPATCH


async def test_cooldown_NOT_rolled_back_on_network_failure():
    """Genuine worker reach attempts keep the cooldown to prevent flooding."""
    stub = _StubClient(
        raises=WorkerDispatchError(
            "timeout", error_code="WORKER_TIMEOUT"
        )
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
    await tool.execute(
        _ctx(brief="b", channel_id="c", user_message_id="msg-99")
    )
    assert stub.calls[0][0]["user_message_id"] == "msg-99"
