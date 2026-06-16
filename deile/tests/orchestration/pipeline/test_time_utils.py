"""Unit tests for ``_time_utils`` ISO-8601 UTC helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from deile.orchestration.pipeline._time_utils import (
    format_iso_utc,
    now_utc,
    parse_iso_utc,
)


def test_now_utc_returns_utc_aware():
    dt = now_utc()
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_round_trip_now_utc_format_parse():
    dt = now_utc().replace(microsecond=0)  # canonical format drops micros
    s = format_iso_utc(dt)
    assert s is not None and s.endswith("Z")
    back = parse_iso_utc(s)
    assert back == dt


def test_parse_iso_utc_passthrough_on_none():
    assert parse_iso_utc(None) is None


def test_format_iso_utc_promotes_naive_to_utc():
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert naive.tzinfo is None
    s = format_iso_utc(naive)
    assert s == "2026-01-02T03:04:05Z"


def test_parse_iso_utc_raises_value_error_on_bad_type():
    with pytest.raises(ValueError):
        parse_iso_utc(12345)


def test_parse_iso_utc_handles_z_suffix():
    dt = parse_iso_utc("2026-05-19T12:00:00Z")
    assert dt == datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_utc_rejects_double_z_suffix():
    """Only one trailing Z is stripped — ``...ZZ`` must still raise.

    Python 3.11+ ``datetime.fromisoformat`` natively accepts a single
    trailing ``Z``, so after the strip the helper must explicitly reject
    any remaining ``Z`` suffix — otherwise ``...ZZ`` would silently
    parse as midnight UTC.
    """
    with pytest.raises(ValueError, match="invalid ISO datetime"):
        parse_iso_utc("2026-05-19T12:00:00ZZ")


def test_parse_iso_utc_rejects_triple_z_suffix():
    with pytest.raises(ValueError, match="invalid ISO datetime"):
        parse_iso_utc("2026-05-19T12:00:00ZZZ")


def test_parse_iso_utc_rejects_empty_string():
    with pytest.raises(ValueError, match="invalid ISO datetime"):
        parse_iso_utc("")


def test_parse_iso_utc_rejects_bare_z():
    with pytest.raises(ValueError, match="invalid ISO datetime"):
        parse_iso_utc("Z")


def test_format_iso_utc_converts_aware_to_utc():
    """Aware datetime in a non-UTC zone must be converted to UTC."""
    brt = timezone(offset=timedelta(hours=-3))
    dt = datetime(2026, 1, 2, 6, 4, 5, tzinfo=brt)  # 06:04 BRT = 09:04 UTC
    s = format_iso_utc(dt)
    assert s == "2026-01-02T09:04:05Z"
