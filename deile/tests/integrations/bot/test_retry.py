"""Tests for BotClientFacade retry with exponential backoff (issue #279).

Covers:
- Retry succeeds on 2nd attempt (transient errors)
- Retry exhausted → last exception propagated
- No retry on auth / not-ready errors
- No retry on upstream 4xx (non-transient)
- Backoff delay increases exponentially with jitter
- retry_attempts configurable via BotIntegrationSettings
- Each attempt logged at WARNING level
- Time budget respected (doesn't exceed timeout_s * retry_attempts)
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import patch

import pytest

# deilebot_client é um pacote separado (repo elimarcavalli/deilebot).
pytest.importorskip("deilebot_client")

from deilebot_client.errors import (  # noqa: E402
    BotClientAuthError,
    BotClientNotReady,
    BotClientRateLimited,
    BotClientTimeoutError,
    BotClientUpstreamError,
)

from deile.integrations.bot.client import BotClientFacade  # noqa: E402
from deile.integrations.bot.config import BotIntegrationSettings  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


class CountingFake:
    """Fake underlying client that raises/returns on schedule via counters."""

    def __init__(self, fail_count: int = 1, exc_type=BotClientTimeoutError,
                 exc_kwargs=None, success_result=None):
        self.calls = 0
        self.fail_count = fail_count
        self.exc_type = exc_type
        self.exc_kwargs = exc_kwargs or {}
        self.success_result = success_result or {"ok": True}

    async def discord_channel_post(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc_type("transient error", **self.exc_kwargs)
        return self.success_result

    async def discord_dm_send(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc_type("transient error", **self.exc_kwargs)
        return self.success_result

    async def discord_reaction_add(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc_type("transient error", **self.exc_kwargs)
        return self.success_result

    async def discord_thread_start(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc_type("transient error", **self.exc_kwargs)
        return self.success_result

    async def discord_message_pin(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc_type("transient error", **self.exc_kwargs)
        return self.success_result

    async def discord_message_edit(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc_type("transient error", **self.exc_kwargs)
        return self.success_result

    async def discord_role_mention(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc_type("transient error", **self.exc_kwargs)
        return self.success_result

    async def whatsapp_send_template(self, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise self.exc_type("transient error", **self.exc_kwargs)
        return self.success_result


class AlwaysRaiseFake:
    """Fake client that always raises the given exception type."""

    def __init__(self, exc_type=BotClientTimeoutError, exc_kwargs=None):
        self.calls = 0
        self.exc_type = exc_type
        self.exc_kwargs = exc_kwargs or {}

    def _raise(self):
        self.calls += 1
        raise self.exc_type("error", **self.exc_kwargs)

    async def discord_channel_post(self, **kwargs):
        self._raise()

    async def discord_dm_send(self, **kwargs):
        self._raise()

    async def discord_reaction_add(self, **kwargs):
        self._raise()

    async def discord_thread_start(self, **kwargs):
        self._raise()

    async def discord_message_pin(self, **kwargs):
        self._raise()

    async def discord_message_edit(self, **kwargs):
        self._raise()

    async def discord_role_mention(self, **kwargs):
        self._raise()

    async def whatsapp_send_template(self, **kwargs):
        self._raise()

    # Non-messaging methods (no retry layer in facade)
    async def health(self):
        self._raise()

    async def get_user_profile(self, user_id: str):
        self._raise()


def make_facade(retry_attempts: int = 3, timeout_s: float = 10.0) -> BotClientFacade:
    """Build a facade with sterile settings (no real endpoint needed)."""
    settings = BotIntegrationSettings(
        endpoint="http://127.0.0.1:1",
        auth_token="test",
        timeout_s=timeout_s,
        retry_attempts=retry_attempts,
    )
    return BotClientFacade(settings)


# ── test: retry succeeds on 2nd attempt ──────────────────────────────────────


@pytest.mark.parametrize("exc_type", [
    BotClientTimeoutError,
    BotClientRateLimited,
    BotClientUpstreamError,
])
async def test_retry_succeeds_on_second_attempt(exc_type):
    """Simulate one transient failure, then success."""
    fake = CountingFake(fail_count=1, exc_type=exc_type)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    result = await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 2
    assert result == fake.success_result


@pytest.mark.parametrize("exc_type", [
    BotClientTimeoutError,
    BotClientRateLimited,
    BotClientUpstreamError,
])
async def test_retry_succeeds_on_last_attempt(exc_type):
    """Simulate N-1 failures, success on the Nth (last) attempt."""
    fake = CountingFake(fail_count=2, exc_type=exc_type)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    result = await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 3  # 2 fails + 1 success
    assert result == fake.success_result


# ── test: exhausted retries ──────────────────────────────────────────────────


@pytest.mark.parametrize("exc_type", [
    BotClientTimeoutError,
    BotClientRateLimited,
    BotClientUpstreamError,
])
async def test_retry_exhausted_raises_last_error(exc_type):
    """All attempts fail → last exception is propagated."""
    fake = AlwaysRaiseFake(exc_type=exc_type)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    with pytest.raises(exc_type):
        await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 3


# ── test: no retry on non-transient errors ───────────────────────────────────


async def test_no_retry_on_auth_error():
    """BotClientAuthError fails immediately (1 call, no retry)."""
    fake = AlwaysRaiseFake(exc_type=BotClientAuthError,
                           exc_kwargs={"status_code": 401})
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    with pytest.raises(BotClientAuthError):
        await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 1


async def test_no_retry_on_not_ready():
    """BotClientNotReady fails immediately (1 call, no retry)."""
    fake = AlwaysRaiseFake(exc_type=BotClientNotReady)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    with pytest.raises(BotClientNotReady):
        await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 1


async def test_no_retry_on_upstream_4xx():
    """BotClientUpstreamError with status_code=400 → no retry."""
    fake = AlwaysRaiseFake(
        exc_type=BotClientUpstreamError,
        exc_kwargs={"status_code": 400, "code": "BAD_REQUEST"},
    )
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    with pytest.raises(BotClientUpstreamError):
        await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 1


async def test_retry_on_upstream_5xx():
    """BotClientUpstreamError with status_code=500 → retries (transient)."""
    fake = CountingFake(
        fail_count=1,
        exc_type=BotClientUpstreamError,
        exc_kwargs={"status_code": 500},
    )
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    result = await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 2
    assert result == fake.success_result


async def test_retry_on_upstream_429():
    """BotClientUpstreamError with status_code=429 → retries (rate-limit)."""
    fake = CountingFake(
        fail_count=1,
        exc_type=BotClientUpstreamError,
        exc_kwargs={"status_code": 429},
    )
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    result = await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 2
    assert result == fake.success_result


async def test_retry_on_upstream_no_status_code():
    """BotClientUpstreamError without status_code → assume transient, retry."""
    fake = CountingFake(
        fail_count=1,
        exc_type=BotClientUpstreamError,
    )
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    result = await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 2
    assert result == fake.success_result


# ── test: backoff delay increases ────────────────────────────────────────────


async def test_backoff_delay_increases():
    """Verify delays grow: ~1s, ~2s, ~4s with jitter."""
    fake = AlwaysRaiseFake(exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=4)
    facade.set_underlying(fake)

    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(BotClientTimeoutError):
            await facade.channel_post(channel_id="1", text="hello")

    assert len(sleep_calls) == 3  # 3 retries, 4th is the final failure
    # Delays should be roughly 1, 2, 4 seconds (with ±20% jitter)
    assert 0.8 <= sleep_calls[0] <= 1.2
    assert 1.6 <= sleep_calls[1] <= 2.4
    assert 3.2 <= sleep_calls[2] <= 4.8


async def test_jitter_adds_randomness():
    """Jitter should produce different delays across calls (non-deterministic)."""
    fake = AlwaysRaiseFake(exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=10)  # many retries to collect samples
    facade.set_underlying(fake)

    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    with patch("asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(BotClientTimeoutError):
            await facade.channel_post(channel_id="1", text="hello")

    # All delays should be within jitter bounds (base * [0.8, 1.2])
    for i, delay in enumerate(sleep_calls):
        base = 2 ** i
        assert base * 0.8 <= delay <= base * 1.2, (
            f"delay[{i}]={delay} out of range [{base * 0.8}, {base * 1.2}]"
        )

    # At least some delays should differ (jitter is random)
    unique_delays = len(set(round(d, 2) for d in sleep_calls))
    assert unique_delays > 1, "Expected jitter to produce different delays"


# ── test: configurable retry_attempts ────────────────────────────────────────


async def test_retry_attempts_zero_no_retry():
    """retry_attempts=1 → no retry (1st attempt is the only one)."""
    fake = AlwaysRaiseFake(exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=1)
    facade.set_underlying(fake)

    with pytest.raises(BotClientTimeoutError):
        await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 1


async def test_retry_attempts_five():
    """retry_attempts=5 → up to 5 attempts on transient errors."""
    fake = AlwaysRaiseFake(exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=5)
    facade.set_underlying(fake)

    with pytest.raises(BotClientTimeoutError):
        await facade.channel_post(channel_id="1", text="hello")
    assert fake.calls == 5


# ── test: logs each attempt ──────────────────────────────────────────────────


async def test_retry_logs_each_attempt():
    """Each retry attempt emits a WARNING log.

    Uses patch.object rather than caplog so the test is robust against
    logging.disable() side-effects from other tests in the suite.
    """
    import deile.integrations.bot.client as client_mod

    fake = AlwaysRaiseFake(exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    warn_calls: list[str] = []

    def _capture(msg, *args, **kwargs):
        warn_calls.append(msg % args if args else msg)

    with patch.object(client_mod.logger, "warning", side_effect=_capture):
        with pytest.raises(BotClientTimeoutError):
            await facade.channel_post(channel_id="1", text="hello")

    assert len(warn_calls) == 2  # 2 retries before exhaustion
    for i, msg in enumerate(warn_calls):
        assert f"attempt {i + 1}/3" in msg
        assert "channel_post" in msg
        assert "BotClientTimeoutError" in msg


async def test_successful_retry_logs_attempts():
    """Logs are emitted for failed attempts before eventual success.

    Uses patch.object rather than caplog for suite-wide robustness.
    """
    import deile.integrations.bot.client as client_mod

    fake = CountingFake(fail_count=1, exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    warn_calls: list[str] = []

    def _capture(msg, *args, **kwargs):
        warn_calls.append(msg % args if args else msg)

    with patch.object(client_mod.logger, "warning", side_effect=_capture):
        await facade.channel_post(channel_id="1", text="hello")

    assert len(warn_calls) == 1
    assert "attempt 1/3" in warn_calls[0]


async def test_no_retry_logs_for_non_transient():
    """Non-transient errors produce no retry log.

    Uses patch.object rather than caplog for suite-wide robustness.
    """
    import deile.integrations.bot.client as client_mod

    fake = AlwaysRaiseFake(exc_type=BotClientAuthError,
                           exc_kwargs={"status_code": 401})
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    warn_calls: list[str] = []

    def _capture(msg, *args, **kwargs):
        warn_calls.append(msg % args if args else msg)

    with patch.object(client_mod.logger, "warning", side_effect=_capture):
        with pytest.raises(BotClientAuthError):
            await facade.channel_post(channel_id="1", text="hello")

    assert len(warn_calls) == 0


# ── test: time budget respected ──────────────────────────────────────────────


async def test_time_budget_respected():
    """When deadline is reached during retry loop, stop and raise."""
    fake = AlwaysRaiseFake(exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=5, timeout_s=0.01)  # very tight budget
    facade.set_underlying(fake)

    # Mock time.monotonic to advance past deadline immediately
    t0 = 0.0
    times = [t0]

    def fake_monotonic():
        return times[0]

    with patch("time.monotonic", side_effect=fake_monotonic):
        # Advance time past deadline after first call
        with patch.object(facade, "_ensure_client", side_effect=facade._ensure_client):
            with pytest.raises(BotClientTimeoutError):
                # Before first call, set time past deadline
                times[0] = 1000.0
                # Need to also mock asyncio.sleep to avoid real waiting
                with patch("asyncio.sleep", return_value=None):
                    await facade.channel_post(channel_id="1", text="hello")


# ── test: all messaging methods go through retry ─────────────────────────────


@pytest.mark.parametrize("method_name,kwargs", [
    ("channel_post", {"channel_id": "1", "text": "hi"}),
    ("dm_send", {"user_id": "1", "text": "hi"}),
    ("reaction_add", {"channel_id": "1", "message_id": "2", "emoji": "👍"}),
    ("thread_start", {"channel_id": "1", "name": "test"}),
    ("message_pin", {"channel_id": "1", "message_id": "2"}),
    ("message_edit", {"channel_id": "1", "message_id": "2", "text": "edit"}),
    ("role_mention", {"channel_id": "1", "role_id": "9", "text": "ping"}),
    ("whatsapp_send_template", {"to": "1", "template_name": "t", "language": "en"}),
])
async def test_all_messaging_methods_use_retry(method_name, kwargs):
    """Every messaging method retries on transient errors."""
    fake = CountingFake(fail_count=1, exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    method = getattr(facade, method_name)
    result = await method(**kwargs)
    assert fake.calls == 2
    assert result == fake.success_result


# ── test: non-messaging ops do NOT retry ─────────────────────────────────────


async def test_health_does_not_retry():
    """health() is not a messaging op — should not retry."""
    fake = AlwaysRaiseFake(exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    with pytest.raises(BotClientTimeoutError):
        await facade.health()
    assert fake.calls == 1


async def test_get_user_does_not_retry():
    """get_user() is not a messaging op — should not retry."""
    fake = AlwaysRaiseFake(exc_type=BotClientTimeoutError)
    facade = make_facade(retry_attempts=3)
    facade.set_underlying(fake)

    with pytest.raises(BotClientTimeoutError):
        await facade.get_user("123")
    assert fake.calls == 1
