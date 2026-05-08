"""Unit tests for `deilebot_client.BotControlClient`.

We hit a real-but-tiny aiohttp server (the actual control-plane wired
to a fake adapter) for happy paths and use a stub aiohttp app for the
auth/timeout/5xx cases — that's the same level of mocking we'd use in
prod, and it catches contract drift between client and server.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Tuple

import pytest
from aiohttp import web
from deilebot_client import (BotClientAuthError, BotClientNotReady,
                             BotClientRateLimited, BotClientTimeoutError,
                             BotClientUpstreamError, BotControlClient,
                             BotControlSettings)

# --- helpers -----------------------------------------------------------------


async def _start(app: web.Application) -> Tuple[web.AppRunner, int]:
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    sock = site._server.sockets[0]
    return runner, sock.getsockname()[1]


# --- happy paths against a stub aiohttp server -------------------------------


@pytest.fixture
async def stub_server() -> AsyncIterator[Tuple[BotControlClient, int]]:
    routes = web.RouteTableDef()

    @routes.get("/v1/health")
    async def _h(_):
        return web.json_response(
            {"ok": True, "version": "stub", "providers": ["discord"], "is_ready": True}
        )

    @routes.post("/v1/outbound/discord/channel.post")
    async def _post(req):
        body = await req.json()
        return web.json_response(
            {
                "message_id": "m-" + body["channel_id"],
                "channel_id": body["channel_id"],
                "sent_at": "2025-01-01T00:00:00+00:00",
            }
        )

    @routes.post("/v1/outbound/discord/dm.send")
    async def _dm(req):
        body = await req.json()
        return web.json_response(
            {
                "message_id": "dm-1",
                "user_id": body.get("user_id") or "resolved",
                "sent_at": "2025-01-01T00:00:00+00:00",
            }
        )

    @routes.post("/v1/outbound/discord/reaction.add")
    async def _react(_):
        return web.json_response({"ok": True})

    @routes.post("/v1/outbound/discord/thread.start")
    async def _thread(req):
        body = await req.json()
        return web.json_response({"thread_id": "t-1", "name": body["name"]})

    @routes.post("/v1/outbound/discord/message.pin")
    async def _pin(_):
        return web.json_response({"ok": True})

    @routes.post("/v1/outbound/discord/role.mention")
    async def _mention(_):
        return web.json_response({"message_id": "rm-1"})

    @routes.get("/v1/users/{uid}")
    async def _user(req):
        return web.json_response(
            {
                "user_id": req.match_info["uid"],
                "username": "elimar.ciss",
                "display_name": "Elimar",
                "avatar_url": None,
                "is_bot": False,
            }
        )

    app = web.Application()
    app.add_routes(routes)
    runner, port = await _start(app)
    settings = BotControlSettings(
        endpoint=f"http://127.0.0.1:{port}", auth_token="x", timeout_s=2.0, retry_attempts=1
    )
    client = BotControlClient(settings)
    try:
        yield client, port
    finally:
        await client.aclose()
        await runner.cleanup()


async def test_health_returns_envelope(stub_server):
    client, _ = stub_server
    h = await client.health()
    assert h.ok is True
    assert "discord" in h.providers


async def test_channel_post_round_trip(stub_server):
    client, _ = stub_server
    res = await client.discord_channel_post(channel_id="42", text="hello")
    assert res.message_id == "m-42"
    assert res.channel_id == "42"


async def test_dm_send_uses_user_id(stub_server):
    client, _ = stub_server
    res = await client.discord_dm_send(user_id="42", text="hi")
    assert res.user_id == "42"


async def test_dm_send_rejects_both_ids(stub_server):
    client, _ = stub_server
    with pytest.raises(Exception):
        await client.discord_dm_send(user_id="42", bot_user_id="zzz", text="hi")


async def test_reaction_thread_pin_mention_user(stub_server):
    client, port = stub_server
    assert (await client.discord_reaction_add(channel_id="1", message_id="2", emoji="👍")).ok is True
    th = await client.discord_thread_start(channel_id="1", name="hot-thread")
    assert th.thread_id == "t-1"
    pin = await client.discord_message_pin(channel_id="1", message_id="2")
    assert pin.ok is True
    rm = await client.discord_role_mention(channel_id="1", role_id="9", text="ping")
    assert rm.message_id == "rm-1"
    user = await client.get_user_profile("123")
    assert user.username == "elimar.ciss"


# --- error paths -------------------------------------------------------------


@pytest.fixture
async def auth_required_server() -> AsyncIterator[int]:
    routes = web.RouteTableDef()

    @routes.post("/v1/outbound/discord/channel.post")
    async def _post(req):
        if req.headers.get("Authorization") != "Bearer goodtoken":
            return web.json_response(
                {"error": {"code": "UNAUTHORIZED", "message": "bad token", "details": {}}},
                status=401,
            )
        return web.json_response(
            {"message_id": "m1", "channel_id": "c1", "sent_at": "2025-01-01T00:00:00+00:00"}
        )

    app = web.Application()
    app.add_routes(routes)
    runner, port = await _start(app)
    try:
        yield port
    finally:
        await runner.cleanup()


async def test_auth_failure_raises_typed(auth_required_server):
    settings = BotControlSettings(
        endpoint=f"http://127.0.0.1:{auth_required_server}", auth_token="wrongtoken",
        timeout_s=2.0, retry_attempts=1,
    )
    async with BotControlClient(settings) as cli:
        with pytest.raises(BotClientAuthError) as exc:
            await cli.discord_channel_post(channel_id="1", text="x")
        assert exc.value.code == "UNAUTHORIZED"
        assert exc.value.status_code == 401


@pytest.fixture
async def slow_server() -> AsyncIterator[int]:
    routes = web.RouteTableDef()

    @routes.post("/v1/outbound/discord/channel.post")
    async def _slow(_):
        await asyncio.sleep(2.0)
        return web.json_response({"message_id": "x", "channel_id": "x", "sent_at": "2025-01-01T00:00:00+00:00"})

    app = web.Application()
    app.add_routes(routes)
    runner, port = await _start(app)
    try:
        yield port
    finally:
        await runner.cleanup()


async def test_timeout_raises_typed(slow_server):
    settings = BotControlSettings(
        endpoint=f"http://127.0.0.1:{slow_server}", auth_token="x",
        timeout_s=0.2, retry_attempts=1,
    )
    async with BotControlClient(settings) as cli:
        with pytest.raises(BotClientTimeoutError):
            await cli.discord_channel_post(channel_id="1", text="x")


@pytest.fixture
async def flaky_server() -> AsyncIterator[Tuple[int, dict]]:
    """500 the first N times, then 200. Lets us verify retry behaviour."""
    state = {"count": 0, "fail_first": 2}
    routes = web.RouteTableDef()

    @routes.post("/v1/outbound/discord/channel.post")
    async def _flaky(_):
        state["count"] += 1
        if state["count"] <= state["fail_first"]:
            return web.json_response(
                {"error": {"code": "INTERNAL_ERROR", "message": "boom", "details": {}}},
                status=500,
            )
        return web.json_response(
            {"message_id": "ok", "channel_id": "1", "sent_at": "2025-01-01T00:00:00+00:00"}
        )

    app = web.Application()
    app.add_routes(routes)
    runner, port = await _start(app)
    try:
        yield port, state
    finally:
        await runner.cleanup()


async def test_5xx_retries_then_succeeds(flaky_server):
    port, state = flaky_server
    settings = BotControlSettings(
        endpoint=f"http://127.0.0.1:{port}", auth_token="x",
        timeout_s=2.0, retry_attempts=4,
    )
    async with BotControlClient(settings) as cli:
        res = await cli.discord_channel_post(channel_id="1", text="x")
    assert res.message_id == "ok"
    assert state["count"] == state["fail_first"] + 1


async def test_5xx_exhausts_retries_then_raises(flaky_server):
    port, state = flaky_server
    state["fail_first"] = 99  # always fail
    settings = BotControlSettings(
        endpoint=f"http://127.0.0.1:{port}", auth_token="x",
        timeout_s=2.0, retry_attempts=2,
    )
    async with BotControlClient(settings) as cli:
        with pytest.raises(BotClientUpstreamError):
            await cli.discord_channel_post(channel_id="1", text="x")
    assert state["count"] >= 2


@pytest.fixture
async def rate_limit_server() -> AsyncIterator[int]:
    routes = web.RouteTableDef()

    @routes.post("/v1/outbound/discord/channel.post")
    async def _rl(_):
        return web.json_response(
            {"error": {"code": "RATE_LIMITED", "message": "slow down", "details": {}}},
            status=429,
            headers={"Retry-After": "3"},
        )

    app = web.Application()
    app.add_routes(routes)
    runner, port = await _start(app)
    try:
        yield port
    finally:
        await runner.cleanup()


async def test_rate_limit_carries_retry_after(rate_limit_server):
    settings = BotControlSettings(
        endpoint=f"http://127.0.0.1:{rate_limit_server}", auth_token="x",
        timeout_s=2.0, retry_attempts=1,
    )
    async with BotControlClient(settings) as cli:
        with pytest.raises(BotClientRateLimited) as exc:
            await cli.discord_channel_post(channel_id="1", text="x")
        assert exc.value.retry_after_s == 3.0


async def test_503_maps_to_not_ready():
    """Synthetic check: response with NOT_READY code should map cleanly."""
    routes = web.RouteTableDef()

    @routes.post("/v1/outbound/discord/channel.post")
    async def _ready(_):
        return web.json_response(
            {"error": {"code": "NOT_READY", "message": "starting", "details": {}}},
            status=503,
        )

    app = web.Application()
    app.add_routes(routes)
    runner, port = await _start(app)
    try:
        settings = BotControlSettings(
            endpoint=f"http://127.0.0.1:{port}", auth_token="x", retry_attempts=1
        )
        async with BotControlClient(settings) as cli:
            with pytest.raises(BotClientNotReady):
                await cli.discord_channel_post(channel_id="1", text="x")
    finally:
        await runner.cleanup()


async def test_invalid_user_id_rejected_locally():
    """The client validates user_id without making a network call."""
    settings = BotControlSettings(endpoint="http://127.0.0.1:1", auth_token="x")
    async with BotControlClient(settings) as cli:
        with pytest.raises(ValueError):
            await cli.get_user_profile("../bad")


async def test_settings_repr_masks_token():
    settings = BotControlSettings(endpoint="http://x", auth_token="topsecret")
    rep = repr(settings)
    assert "topsecret" not in rep
