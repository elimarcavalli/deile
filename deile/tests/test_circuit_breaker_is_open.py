"""Regression test for ``CircuitBreaker.is_open`` side-effect.

Bug: ``is_open`` used to delegate to ``allow_request``, which transitions
OPEN→HALF_OPEN as a side effect, consuming the single probe slot.
``TierRouter.select`` called ``is_open`` once per cascade entry; with
duplicate provider_ids in a tier (the YAML allows them — e.g. tier_3
lists gemini twice), the first ``is_open`` call would burn the probe
before a cascade entry that actually targeted that provider could grab
it, leaving the breaker stuck OPEN for another full cooldown.

The fix: ``is_open`` is now read-only; ``TierRouter.select`` calls
``allow_request`` only at commit time on the chosen cascade entry.
"""

from __future__ import annotations

import time

from deile.core.models.tier_router import (BreakerState, CircuitBreaker,
                                           _ProviderBreaker)


def test_is_open_does_not_transition_state_after_cooldown() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.05)
    cb.record_failure("openai")
    assert cb.get_state("openai") == BreakerState.OPEN

    time.sleep(0.06)  # cooldown elapsed

    # First call: should report "not open" (probe available) without mutating.
    assert cb.is_open("openai") is False
    assert cb.get_state("openai") == BreakerState.OPEN  # NOT half-open yet

    # Second call (e.g. another cascade entry for same provider_id):
    # must also report "not open" — the previous fix would have moved to
    # HALF_OPEN on the first call, leaving the second call to see HALF_OPEN
    # and return False (still OK), but the semantics are now strictly
    # side-effect-free.
    assert cb.is_open("openai") is False
    assert cb.get_state("openai") == BreakerState.OPEN


def test_allow_request_still_transitions_after_cooldown() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.05)
    cb.record_failure("openai")
    time.sleep(0.06)

    # The commit-time call should transition.
    assert cb.allow_request("openai") is True
    assert cb.get_state("openai") == BreakerState.HALF_OPEN


def test_is_open_during_cooldown_returns_true() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=5.0)
    cb.record_failure("openai")
    assert cb.is_open("openai") is True


def test_is_open_for_closed_provider() -> None:
    cb = CircuitBreaker()
    assert cb.is_open("openai") is False
    assert cb.is_open("unknown") is False


def test_is_open_in_half_open_returns_false() -> None:
    cb = CircuitBreaker()
    cb._breakers["openai"] = _ProviderBreaker(state=BreakerState.HALF_OPEN)
    assert cb.is_open("openai") is False


def test_repeated_is_open_calls_do_not_leak_probe() -> None:
    """The whole point of the fix: a duplicate-provider cascade must not
    waste the probe by burning it on the first cascade entry that finds
    no registered provider."""
    cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.05)
    cb.record_failure("openai")
    time.sleep(0.06)

    # Caller checks the same provider_id repeatedly (simulating cascade
    # iteration over duplicate keys). State must NOT advance.
    for _ in range(5):
        assert cb.is_open("openai") is False
        assert cb.get_state("openai") == BreakerState.OPEN

    # Eventually the caller commits via allow_request — only THEN do we
    # transition.
    assert cb.allow_request("openai") is True
    assert cb.get_state("openai") == BreakerState.HALF_OPEN
